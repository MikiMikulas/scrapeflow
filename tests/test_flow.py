"""Flows: filters before download, Markdown conversion, Python code nodes, several storage nodes."""

import json

import httpx
import pytest
import respx
from sqlmodel import Session, select

from scrapeflow import flow
from scrapeflow.checker import run_check
from scrapeflow.config import get_settings
from scrapeflow.db import get_engine
from scrapeflow.models import FileOutput, Site, SiteFile

URL = "https://data.test/open/"


def page(*files: str) -> httpx.Response:
    links = "".join(f'<a href="{f}">Dataset {f}</a>' for f in files)
    return httpx.Response(200, text=f"<html><body>{links}</body></html>", headers={"content-type": "text/html"})


def graph(*nodes, edges) -> dict:
    return {"nodes": [{"id": "source", "type": "source", "config": {}}, *nodes],
            "edges": [{"from": a, "out": o, "to": b} for a, o, b in edges]}


def node(id, type, **config):
    return {"id": id, "type": type, "config": config}


def make_site(flow_def) -> int:
    with Session(get_engine()) as s:
        site = Site(name="Open data", url=URL, flow=json.dumps(flow_def))
        s.add(site)
        s.commit()
        return site.id


def outputs(site_id) -> dict[str, FileOutput]:
    with Session(get_engine()) as s:
        rows = s.exec(select(FileOutput).join(SiteFile).where(SiteFile.site_id == site_id)).all()
        return {o.storage_path: o for o in rows}


def stored(path: str) -> bytes:
    return (get_settings().data_dir / "files" / path).read_bytes()


@pytest.fixture
def site_mock():
    with respx.mock:
        respx.get("https://data.test/robots.txt").mock(return_value=httpx.Response(404))
        respx.get(URL).mock(return_value=page("people.csv", "notes.txt", "photo.jpg"))
        respx.get(URL + "people.csv").mock(return_value=httpx.Response(200, content=b"name,age\nEva,31\nJan,40\n"))
        respx.get(URL + "notes.txt").mock(return_value=httpx.Response(200, content=b"hello"))
        photo = respx.get(URL + "photo.jpg").mock(return_value=httpx.Response(200, content=b"jpg"))
        yield photo


async def test_branches_markdown_and_code(site_mock):
    code = ("def process(input_path, meta, output_dir):\n"
            "    print('rows:', len(input_path.read_text().splitlines()))\n"
            "    (output_dir / 'summary.txt').write_text(meta['filename'].upper())\n")
    sid = make_site(graph(
        node("f", "filter", extensions="csv, txt"),
        node("md", "markdown"),
        node("py", "code", code=code),
        node("raw", "storage", folder="{site}/original"),
        node("out", "storage", folder="{site}/{ext}"),
        edges=[("source", "files", "f"), ("f", "match", "raw"), ("f", "match", "md"), ("md", "markdown", "out"),
               ("md", "markdown", "py"), ("py", "out", "out")],
    ))
    run = await run_check(sid)
    assert run.status == "ok", run.error
    assert not site_mock.called  # photo.jpg was filtered out before downloading
    out = outputs(sid)
    assert set(out) == {
        "open-data/original/people.csv", "open-data/md/people.md", "open-data/txt/summary.txt",
        "open-data/original/notes.txt", "open-data/md/notes.md", "open-data/txt/summary-2.txt",
    }
    assert b"| name | age |" in stored("open-data/md/people.md")
    assert stored("open-data/txt/summary.txt") == b"PEOPLE.MD"
    log = [line[2] for line in json.loads(run.log)]
    assert any("[code] rows:" in m for m in log) and any("filtered out by Filter" in m for m in log)
    with Session(get_engine()) as s:
        photo = s.exec(select(SiteFile).where(SiteFile.filename == "photo.jpg")).one()
    assert photo.status == "ignored" and "Filter" in photo.error

    # Reprocessing overwrites the same paths instead of creating copies.
    run = await run_check(sid, reprocess=True)
    assert run.updated_files == 2 and set(outputs(sid)) == set(out)


async def test_code_errors_and_timeouts_fail_the_file_not_the_check(site_mock):
    sid = make_site(graph(
        node("f", "filter", extensions="csv"),
        node("py", "code", code="def process(input_path, meta, output_dir):\n    raise ValueError('bad data')\n"),
        node("out", "storage"),
        edges=[("source", "files", "f"), ("f", "match", "py"), ("py", "out", "out")],
    ))
    run = await run_check(sid)
    assert run.status == "ok" and run.failed == 1
    with Session(get_engine()) as s:
        csv = s.exec(select(SiteFile).where(SiteFile.filename == "people.csv")).one()
    assert csv.status == "failed" and "ValueError: bad data" in csv.error


def test_validation():
    assert flow.validate(flow.default_flow()) == []
    assert flow.validate(flow.default_flow("only invoices")) == []
    bad_code = graph(node("py", "code", code="def process(:\n"), edges=[("source", "files", "py")])
    assert "line 1" in flow.validate(bad_code)[0]
    loop = graph(node("a", "markdown"), node("b", "markdown"),
                 edges=[("source", "files", "a"), ("a", "markdown", "b"), ("b", "markdown", "a")])
    assert flow.validate(loop) == ["The flow contains a loop"]
    assert "exactly one Website" in flow.validate({"nodes": [], "edges": []})[0]
    assert "no output" in flow.validate(graph(node("f", "filter"), edges=[("source", "nope", "f")]))[0]
    assert "No Save to storage" in flow.warnings(graph(node("m", "markdown"), edges=[("source", "files", "m")]))[0]


def test_flow_api_and_dry_run(site_mock):
    from fastapi.testclient import TestClient

    from scrapeflow.web import app

    sid = make_site(flow.default_flow())
    with TestClient(app) as client:
        page = client.get(f"/sites/{sid}/flow")
        assert page.status_code == 200 and "drawflow" in page.text
        data = client.get(f"/api/sites/{sid}/flow").json()
        assert data["flow"]["nodes"][0]["type"] == "source"

        bad = graph(node("py", "code", code="def process(:"), edges=[("source", "files", "py")])
        r = client.put(f"/api/sites/{sid}/flow", json={"flow": bad})
        assert r.status_code == 422 and "line 1" in r.json()["detail"]

        md_flow = graph(node("md", "markdown"), node("out", "storage", folder="{site}/md"),
                        edges=[("source", "files", "md"), ("md", "markdown", "out")])
        assert client.put(f"/api/sites/{sid}/flow", json={"flow": md_flow}).json()["ok"]

        assert client.post(f"/api/sites/{sid}/check").json()["new_files"] == 3
        files = {f["filename"]: f for f in client.get(f"/api/files?site_id={sid}").json()}

        # Dry run with an unsaved code node: nothing is written to storage.
        code = ("def process(input_path, meta, output_dir):\n"
                "    return 'rows: ' + str(len(input_path.read_text().splitlines()))\n")
        test_flow = graph(node("py", "code", code=code), node("out", "storage"),
                          edges=[("source", "files", "py"), ("py", "out", "out")])
        # The stored copy of people.csv is the Markdown table (header, separator, 2 rows).
        r = client.post(f"/api/sites/{sid}/flow/test",
                        json={"file_id": files["people.csv"]["id"], "flow": test_flow})
        body = r.json()
        assert r.status_code == 200, body
        assert body["outputs"] == [{"node": "out", "filename": "people.md", "size": 7, "folder": "{site}",
                                    "preview": "rows: 4", "truncated": False}]
        assert any("stored copy" in line[2] for line in body["log"])
        assert not (get_settings().data_dir / "files" / "open-data" / "people.md").exists()

        detail = client.get(f"/sites/{sid}").text
        assert "/outputs/" in detail and "people.md" in detail  # converted output listed with the original
