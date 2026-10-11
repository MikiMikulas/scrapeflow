import httpx
import pytest
import respx
from sqlmodel import Session

from scrapeflow import storage
from scrapeflow.db import get_engine


async def test_s3_backend_roundtrip(tmp_path, monkeypatch):
    moto = pytest.importorskip("moto")
    import boto3

    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with moto.mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="files")
        backend = storage.get_storage("s3", {"bucket": "files", "access_key": "k", "secret_key": "s",
                                             "region": "us-east-1", "prefix": "scrapeflow/"})
        assert "bucket files" in await backend.test()
        src = tmp_path / "x.pdf"
        src.write_bytes(b"pdf")
        await backend.put("w/x.pdf", src, "application/pdf")
        obj = boto3.client("s3", region_name="us-east-1").get_object(Bucket="files", Key="scrapeflow/w/x.pdf")
        assert obj["Body"].read() == b"pdf" and obj["ContentType"] == "application/pdf"
        assert b"".join([c async for c in backend.open("w/x.pdf")]) == b"pdf"
        missing = storage.get_storage("s3", {"bucket": "nope", "access_key": "k", "secret_key": "s",
                                             "region": "us-east-1"})
        with pytest.raises(storage.StorageError, match="NoSuchBucket"):
            await missing.put("x.pdf", src)


@respx.mock
async def test_webdav_backend_creates_folders_and_uploads(tmp_path):
    base = "https://cloud.test/dav/files/me"
    mkcol = respx.route(method="MKCOL").mock(return_value=httpx.Response(201))
    put = respx.put(f"{base}/ScrapeFlow/w/a%20b.pdf").mock(return_value=httpx.Response(201))
    backend = storage.get_storage("webdav", {"url": base + "/", "username": "me", "password": "pw",
                                             "folder": "ScrapeFlow"})
    src = tmp_path / "f"
    src.write_bytes(b"data")
    await backend.put("w/a b.pdf", src, "application/pdf")
    assert [str(c.request.url) for c in mkcol.calls] == [f"{base}/ScrapeFlow/", f"{base}/ScrapeFlow/w/"]
    assert put.calls[0].request.content == b"data"
    assert put.calls[0].request.headers["authorization"].startswith("Basic ")


def test_storage_settings_are_per_backend():
    with Session(get_engine()) as s:
        storage.save_options(s, "s3", {"bucket": "b1"})
        storage.save_options(s, "local", {"path": "/downloads"})
    active, per = storage.all_options()
    assert active == "local" and per == {"s3": {"bucket": "b1"}, "local": {"path": "/downloads"}}
    assert storage.get_storage().opt("path") == "/downloads"


async def test_local_storage_rejects_path_escape(tmp_path):
    backend = storage.get_storage("local", {"path": str(tmp_path / "root")})
    src = tmp_path / "f"
    src.write_bytes(b"x")
    await backend.put("../../evil.txt", src)  # ".." segments are dropped
    assert (tmp_path / "root" / "evil.txt").exists()
