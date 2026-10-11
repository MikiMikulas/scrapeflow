from datetime import timedelta

import httpx
import respx
from fastapi.testclient import TestClient
from sqlmodel import Session

from scrapeflow import scheduler, storage
from scrapeflow.db import get_engine
from scrapeflow.models import Schedule, Site, utcnow
from scrapeflow.web import app

URL = "https://files.test/docs/"
PAGE = '<html><body><a href="/f/a.pdf">Report A</a></body></html>'


def mock_site():
    respx.get("https://files.test/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(URL).mock(return_value=httpx.Response(200, text=PAGE, headers={"content-type": "text/html"}))
    respx.get("https://files.test/f/a.pdf").mock(return_value=httpx.Response(
        200, content=b"%PDF", headers={"content-type": "application/pdf"}))


def test_pages_render_and_site_lifecycle():
    with TestClient(app) as client:
        home = client.get("/")
        assert home.status_code == 200 and "Add website" in home.text and "Choose storage" in home.text
        assert client.get("/settings/models").status_code == 200
        assert client.get("/settings/storage").status_code == 200
        assert client.get("/settings", follow_redirects=False).headers["location"] == "/settings/models"

        r = client.post("/sites", data={"url": "ftp://nope"})
        assert r.status_code == 422 and "Enter a website address" in r.text

        r = client.post("/sites", data={"url": "files.test/docs/", "schedule": "daily", "download_existing": "on",
                                        "max_depth": "2", "file_types": "pdf"}, follow_redirects=False)
        assert r.status_code == 303
        detail = r.headers["location"]
        site = client.get("/api/sites").json()[0]
        assert site["url"] == URL and site["name"] == "files.test" and site["schedule"] == "daily"
        assert site["max_depth"] == 2 and site["download_existing"]
        assert "Waiting for the first check" in client.get(detail).text

        with respx.mock:
            mock_site()
            run = client.post(f"/api/sites/{site['id']}/check").json()
        assert run["status"] == "ok" and run["new_files"] == 1

        page = client.get(detail).text
        assert "a.pdf" in page and "Report A" in page and "Stored" in page
        activity = client.get(detail + "?tab=activity").text
        assert "+1" in activity and 'id="console"' in activity
        log = client.get(f"/api/sites/{site['id']}/log").json()
        messages = [line[2] for line in log["lines"]]
        assert not log["running"] and messages[0].startswith("Check started")
        assert any("Saved a.pdf" in m for m in messages) and messages[-1].startswith("Done:")
        assert "log" not in client.get(f"/api/sites/{site['id']}/runs").json()[0]
        assert "Remove website" in client.get(detail + "?tab=settings").text
        assert "a.pdf" not in client.get(detail + "?q=nothing").text
        assert "files.test" in client.get("/").text

        (f,) = client.get("/api/files").json()
        dl = client.get(f"/files/{f['id']}/download")
        assert dl.status_code == 200 and dl.content == b"%PDF" and "a.pdf" in dl.headers["content-disposition"]

        r = client.post(detail, data={"url": URL, "name": "Docs", "schedule": "manual", "max_depth": "1",
                                      "max_pages": "50"}, follow_redirects=False)
        assert r.status_code == 303
        site = client.get(f"/api/sites/{site['id']}").json()
        assert site["name"] == "Docs" and site["schedule"] == "manual" and not site["active"]

        client.post(detail + "/delete")
        assert client.get("/api/sites").json() == [] and client.get("/api/files").json() == []


def test_storage_settings_form_api_and_test():
    with TestClient(app) as client:
        client.post("/settings/storage", data={"preset": "r2", "backend": "s3", "bucket": "b",
                                               "endpoint_url": "https://acc.r2.cloudflarestorage.com",
                                               "access_key": "AK", "secret_key": "SECRET"})
        page = client.get("/settings/storage").text
        assert "Cloudflare R2" in page and "SECRET" not in page
        data = client.get("/api/settings/storage").json()
        assert data["backend"] == "s3" and data["options"]["secret_key"] == "••••"

        # an empty secret keeps the stored one
        client.post("/settings/storage", data={"preset": "r2", "backend": "s3", "bucket": "b2",
                                               "access_key": "AK"})
        with Session(get_engine()) as s:
            saved = storage.load_options(s)[1]
        assert saved["secret_key"] == "SECRET" and saved["bucket"] == "b2"

        client.post("/settings/storage", data={"preset": "local", "backend": "local", "path": ""})
        r = client.post("/api/storage/test", json={"backend": "local", "options": {}})
        assert r.status_code == 200 and r.json()["ok"]
        r = client.post("/api/storage/test", json={"backend": "webdav", "options": {}})
        assert r.status_code == 502 and "Missing settings" in r.json()["detail"]


def test_api_create_and_schedule():
    with TestClient(app) as client:
        r = client.post("/api/sites", json={"url": URL, "schedule": "weekly"})
        assert r.status_code == 201 and r.json()["name"] == "files.test"
    site = Site(name="x", url=URL, schedule=Schedule.WEEKLY)
    assert scheduler.next_run(site) <= utcnow()  # never checked → due now
    site.last_checked_at = utcnow() - timedelta(days=2)
    assert scheduler.next_run(site) > utcnow()
    site.schedule = Schedule.MANUAL
    assert scheduler.next_run(site) is None


def test_new_site_is_checked_in_the_background(monkeypatch):
    import time
    from types import SimpleNamespace

    monkeypatch.setattr(scheduler, "scheduler", SimpleNamespace(running=True, shutdown=lambda wait=False: None))
    with TestClient(app) as client, respx.mock:
        mock_site()
        sid = client.post("/api/sites", json={"url": URL}).json()["id"]
        sid2 = int(client.post("/sites", data={"url": URL}, follow_redirects=False)
                   .headers["location"].rsplit("/", 1)[1])
        for _ in range(100):
            states = [client.get(f"/api/sites/{i}/status").json() for i in (sid, sid2)]
            if not any(s["running"] for s in states) and all(s["last_run"] for s in states):
                break
            time.sleep(0.05)
        assert [s["last_run"]["status"] for s in states] == ["ok", "ok"]


def test_deleting_a_site_cancels_its_check(monkeypatch):
    import asyncio
    import time
    from types import SimpleNamespace

    async def slow_page(request):
        await asyncio.sleep(5)
        return httpx.Response(200, text=PAGE, headers={"content-type": "text/html"})

    monkeypatch.setattr(scheduler, "scheduler", SimpleNamespace(running=True, shutdown=lambda wait=False: None))
    with TestClient(app) as client, respx.mock:
        respx.get("https://files.test/robots.txt").mock(return_value=httpx.Response(404))
        respx.get(URL).mock(side_effect=slow_page)
        sid = client.post("/api/sites", json={"url": URL}).json()["id"]
        time.sleep(0.2)
        assert client.get(f"/api/sites/{sid}/status").json()["running"]
        started = time.monotonic()
        assert client.delete(f"/api/sites/{sid}").status_code == 204
        assert time.monotonic() - started < 2  # cancelled, not waited for
        assert client.get("/api/sites").json() == [] and client.get("/api/files").json() == []
        new = client.post("/api/sites", json={"url": URL, "schedule": "manual"}).json()["id"]
        assert new != sid  # ids of deleted sites are never reused
