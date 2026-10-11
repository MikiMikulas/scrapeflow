"""Checks end to end: crawl → store new files → update changed ones (local storage, mocked HTTP)."""

import httpx
import respx
from sqlmodel import Session, select

from scrapeflow import llm
from scrapeflow.checker import run_check
from scrapeflow.config import get_settings
from scrapeflow.db import get_engine
from scrapeflow.models import Site, SiteFile

URL = "https://urad.test/deska/"
XLSX = {"content-disposition": "attachment; filename*=UTF-8''Rozpo%C4%8Det%202026.xlsx"}


def page(*files: str) -> httpx.Response:
    items = "".join(f'<li><a href="{f}">Dokument {f}</a></li>' for f in files)
    return httpx.Response(200, text=f"<html><body><ul>{items}</ul></body></html>",
                          headers={"content-type": "text/html"})


def make_site(**kw) -> int:
    with Session(get_engine()) as s:
        site = Site(name="Úřední deska", url=URL, **kw)
        s.add(site)
        s.commit()
        return site.id


def files(site_id) -> dict[str, SiteFile]:
    with Session(get_engine()) as s:
        rows = s.exec(select(SiteFile).where(SiteFile.site_id == site_id).order_by(SiteFile.id)).all()
        return {f.filename: f for f in rows}


def stored_bytes(path: str) -> bytes:
    return (get_settings().data_dir / "files" / path).read_bytes()


@respx.mock
async def test_new_and_changed_files_are_mirrored():
    respx.get("https://urad.test/robots.txt").mock(return_value=httpx.Response(404))
    sid = make_site()
    listing = respx.get(URL).mock(return_value=page("a.pdf", "b.xlsx"))
    a = respx.get(URL + "a.pdf").mock(return_value=httpx.Response(
        200, content=b"A1", headers={"content-type": "application/pdf", "etag": '"a1"'}))
    respx.get(URL + "b.xlsx").mock(return_value=httpx.Response(
        200, content=b"B1", headers=XLSX))

    run = await run_check(sid)
    assert run.status == "ok" and run.pages == 1 and run.files_found == 2 and run.new_files == 2
    f = files(sid)
    assert f["a.pdf"].storage_path == "uredni-deska/a.pdf" and f["a.pdf"].version == 1
    assert f["Rozpočet 2026.xlsx"].storage_path == "uredni-deska/Rozpočet 2026.xlsx"
    assert stored_bytes("uredni-deska/a.pdf") == b"A1" and len(f["a.pdf"].sha256) == 64

    # Second check: a.pdf answers 304 to the conditional request, b.xlsx has new content, c.pdf is new.
    listing.mock(return_value=page("a.pdf", "b.xlsx", "c.pdf"))
    a.mock(side_effect=lambda req: httpx.Response(304) if req.headers.get("if-none-match") == '"a1"'
           else httpx.Response(200, content=b"A1"))
    respx.get(URL + "b.xlsx").mock(return_value=httpx.Response(
        200, content=b"B2", headers=XLSX))
    respx.get(URL + "c.pdf").mock(return_value=httpx.Response(200, content=b"C"))
    run = await run_check(sid)
    assert (run.new_files, run.updated_files, run.failed) == (1, 1, 0)
    f = files(sid)
    assert f["Rozpočet 2026.xlsx"].version == 2
    assert stored_bytes("uredni-deska/Rozpočet 2026.xlsx") == b"B2"  # replaced in place
    assert f["a.pdf"].version == 1 and f["c.pdf"].status == "stored"

    # Same content again → nothing stored; a link that disappears is marked, its copy is kept.
    listing.mock(return_value=page("b.xlsx", "c.pdf"))
    run = await run_check(sid)
    assert (run.new_files, run.updated_files) == (0, 0)
    f = files(sid)
    assert not f["a.pdf"].on_site and f["a.pdf"].status == "stored" and f["c.pdf"].on_site
    assert stored_bytes("uredni-deska/a.pdf") == b"A1"


@respx.mock
async def test_failed_downloads_are_retried_and_limits_apply(monkeypatch):
    monkeypatch.setenv("SCRAPEFLOW_MAX_FILE_MB", "1")
    get_settings.cache_clear()
    respx.get("https://urad.test/robots.txt").mock(return_value=httpx.Response(404))
    sid = make_site(storage_folder="archiv/2026")
    respx.get(URL).mock(return_value=page("ok.pdf", "later.pdf", "big.zip"))
    respx.get(URL + "ok.pdf").mock(return_value=httpx.Response(200, content=b"ok"))
    later = respx.get(URL + "later.pdf").mock(return_value=httpx.Response(503))
    respx.get(URL + "big.zip").mock(return_value=httpx.Response(200, content=b"x" * (1024 * 1024 + 1)))

    run = await run_check(sid)
    assert run.status == "ok" and run.new_files == 1 and run.failed == 2
    f = files(sid)
    assert f["ok.pdf"].storage_path == "archiv/2026/ok.pdf"
    assert f["later.pdf"].status == "failed" and "503" in f["later.pdf"].error
    assert "limit" in f["big.zip"].error
    assert list(get_settings().data_dir.glob("scrapeflow-*")) == []  # temp files cleaned up

    later.mock(return_value=httpx.Response(200, content=b"now"))
    run = await run_check(sid)
    assert run.new_files == 1 and files(sid)["later.pdf"].status == "stored"


@respx.mock
async def test_existing_files_can_be_skipped_at_the_first_check():
    respx.get("https://urad.test/robots.txt").mock(return_value=httpx.Response(404))
    sid = make_site(download_existing=False)
    listing = respx.get(URL).mock(return_value=page("old.pdf"))
    old = respx.get(URL + "old.pdf").mock(return_value=httpx.Response(200, content=b"old"))
    run = await run_check(sid)
    assert run.new_files == 0 and files(sid)["old.pdf"].status == "skipped" and not old.called

    listing.mock(return_value=page("old.pdf", "new.pdf"))
    respx.get(URL + "new.pdf").mock(return_value=httpx.Response(200, content=b"new"))
    run = await run_check(sid)
    assert run.new_files == 1 and not old.called


@respx.mock
async def test_ai_filter_ignores_unwanted_files():
    with Session(get_engine()) as s:
        llm.save_config(s, {"provider": "openai", "base_url": "http://llm.test/v1", "model": "m"})
    respx.post("http://llm.test/v1/chat/completions").mock(return_value=httpx.Response(
        200, json={"choices": [{"message": {"content": '{"keep": [1]}'}}]}))
    respx.get("https://urad.test/robots.txt").mock(return_value=httpx.Response(404))
    sid = make_site(ai_filter="only invoices")
    respx.get(URL).mock(return_value=page("faktura.pdf", "menu.pdf"))
    respx.get(URL + "faktura.pdf").mock(return_value=httpx.Response(200, content=b"F"))
    menu = respx.get(URL + "menu.pdf").mock(return_value=httpx.Response(200, content=b"M"))
    run = await run_check(sid)
    assert run.new_files == 1 and not menu.called
    assert files(sid)["menu.pdf"].status == "ignored"


@respx.mock
async def test_unreachable_site_and_broken_storage_are_reported():
    respx.get("https://urad.test/robots.txt").mock(return_value=httpx.Response(404))
    sid = make_site()
    respx.get(URL).mock(return_value=httpx.Response(500))
    run = await run_check(sid)
    assert run.status == "error" and "500" in run.error

    with Session(get_engine()) as s:
        from scrapeflow import storage
        storage.save_options(s, "s3", {"bucket": ""})  # active but incomplete
    respx.get(URL).mock(return_value=page("a.pdf"))
    respx.get(URL + "a.pdf").mock(return_value=httpx.Response(200, content=b"A"))
    run = await run_check(sid)
    assert run.status == "ok" and run.failed == 1
    assert "Missing settings" in files(sid)["a.pdf"].error or "boto3" in files(sid)["a.pdf"].error
