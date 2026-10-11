"""Built-in storage backends: local folder, S3-compatible object storage, WebDAV."""

import asyncio
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import quote

import httpx

from ..config import get_settings
from .base import ConfigField, StorageBackend, StorageError


def _clean(path: str) -> str:
    parts = [p for p in path.replace("\\", "/").split("/") if p not in ("", ".", "..")]
    if not parts:
        raise StorageError("Empty storage path")
    return "/".join(parts)


class LocalStorage(StorageBackend):
    name = "local"
    label = "Local folder"
    description = "A folder inside the container – mount it as a volume to see files on the host."
    fields = [
        ConfigField("path", "Folder", placeholder="default: SCRAPEFLOW_FILES_DIR or /data/files",
                    help="Inside Docker this is a path in the container – see docker-compose.yml to "
                         "save files into a folder on your computer."),
    ]

    @property
    def root(self) -> Path:
        s = get_settings()
        return Path(self.opt("path") or str(s.files_dir or s.data_dir / "files"))

    def _target(self, path: str) -> Path:
        root = self.root.resolve()
        target = (root / _clean(path)).resolve()
        if root not in target.parents:
            raise StorageError("Path escapes the storage folder")
        return target

    async def put(self, path: str, local_file: Path, content_type: str | None = None) -> None:
        target = self._target(path)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(shutil.copyfile, local_file, target)
        except OSError as e:
            raise StorageError(f"Cannot write {target}: {e}") from e

    async def open(self, path: str) -> AsyncIterator[bytes]:
        target = self._target(path)
        if not target.is_file():
            raise StorageError("File not found in storage")
        with target.open("rb") as f:
            while chunk := f.read(1024 * 256):
                yield chunk

    def local_path(self, path: str) -> Path:
        return self._target(path)

    async def delete(self, path: str) -> None:
        self._target(path).unlink(missing_ok=True)

    def describe(self) -> str:
        return f"folder {self.root}"


class S3Storage(StorageBackend):
    name = "s3"
    label = "S3-compatible"
    description = "Amazon S3, MinIO, Cloudflare R2, Backblaze B2, Wasabi, DigitalOcean Spaces…"
    fields = [
        ConfigField("bucket", "Bucket", placeholder="my-files", required=True),
        ConfigField("endpoint_url", "Endpoint URL", placeholder="https://s3.eu-central-1.amazonaws.com",
                    help="Leave empty for AWS. MinIO: http://minio:9000 · R2: https://<account>.r2.cloudflarestorage.com"),
        ConfigField("region", "Region", placeholder="eu-central-1"),
        ConfigField("access_key", "Access key ID", required=True),
        ConfigField("secret_key", "Secret access key", secret=True, required=True, kind="password"),
        ConfigField("prefix", "Key prefix", placeholder="scrapeflow/", help="Optional folder inside the bucket"),
    ]

    def _client(self):
        try:
            import boto3
            from botocore.config import Config
        except ImportError as e:
            raise StorageError("S3 storage needs boto3: pip install 'scrapeflow[s3]'") from e
        return boto3.client(
            "s3",
            endpoint_url=self.opt("endpoint_url") or None,
            region_name=self.opt("region") or None,
            aws_access_key_id=self.opt("access_key") or None,
            aws_secret_access_key=self.opt("secret_key") or None,
            config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
        )

    def _key(self, path: str) -> str:
        prefix = self.opt("prefix").strip("/")
        return f"{prefix}/{_clean(path)}" if prefix else _clean(path)

    async def _call(self, fn, *args, **kwargs):
        from boto3.exceptions import Boto3Error
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except (BotoCoreError, ClientError, Boto3Error) as e:
            raise StorageError(f"S3 error: {e}") from e

    async def put(self, path: str, local_file: Path, content_type: str | None = None) -> None:
        self.validate()
        extra = {"ContentType": content_type} if content_type else None
        await self._call(self._client().upload_file, str(local_file), self.opt("bucket"),
                         self._key(path), ExtraArgs=extra)

    async def open(self, path: str) -> AsyncIterator[bytes]:
        obj = await self._call(self._client().get_object, Bucket=self.opt("bucket"), Key=self._key(path))
        body = obj["Body"]
        try:
            while chunk := await asyncio.to_thread(body.read, 1024 * 256):
                yield chunk
        finally:
            body.close()

    async def delete(self, path: str) -> None:
        await self._call(self._client().delete_object, Bucket=self.opt("bucket"), Key=self._key(path))

    def describe(self) -> str:
        where = self.opt("endpoint_url") or "AWS"
        return f"bucket {self.opt('bucket')} at {where}"


class WebDAVStorage(StorageBackend):
    name = "webdav"
    label = "WebDAV"
    description = "Nextcloud, ownCloud, Synology, QNAP, Box, any WebDAV server."
    fields = [
        ConfigField("url", "WebDAV URL", required=True,
                    placeholder="https://cloud.example.com/remote.php/dav/files/USERNAME/",
                    help="Nextcloud: Files → Settings (bottom left) shows the WebDAV address"),
        ConfigField("username", "Username"),
        ConfigField("password", "Password / app password", secret=True, kind="password"),
        ConfigField("folder", "Folder", placeholder="ScrapeFlow", help="Created automatically"),
    ]

    def _url(self, path: str = "") -> str:
        base = self.opt("url").rstrip("/") + "/"
        parts = [p for p in self.opt("folder").strip("/").split("/") if p]
        if path:
            parts += _clean(path).split("/")
        return base + "/".join(quote(p) for p in parts)

    def _client(self) -> httpx.AsyncClient:
        auth = (self.opt("username"), self.opt("password")) if self.opt("username") else None
        return httpx.AsyncClient(auth=auth, timeout=120, follow_redirects=True)

    async def _ensure_dirs(self, client: httpx.AsyncClient, path: str) -> None:
        parts = [p for p in self.opt("folder").strip("/").split("/") if p] + _clean(path).split("/")[:-1]
        base = self.opt("url").rstrip("/")
        current = ""
        for part in parts:
            current += "/" + quote(part)
            r = await client.request("MKCOL", base + current + "/")
            # 201 created, 405 already exists; some servers answer 301/409 for existing dirs
            if r.status_code not in (201, 301, 405, 409):
                raise StorageError(f"WebDAV MKCOL failed: HTTP {r.status_code}")

    async def put(self, path: str, local_file: Path, content_type: str | None = None) -> None:
        self.validate()
        headers = {"Content-Type": content_type} if content_type else {}
        try:
            async with self._client() as client:
                await self._ensure_dirs(client, path)
                with local_file.open("rb") as f:
                    r = await client.put(self._url(path), content=_iter_file(f), headers=headers)
        except httpx.HTTPError as e:
            raise StorageError(f"WebDAV error: {e}") from e
        if r.status_code not in (200, 201, 204):
            raise StorageError(f"WebDAV upload failed: HTTP {r.status_code}")

    async def open(self, path: str) -> AsyncIterator[bytes]:
        async with self._client() as client, client.stream("GET", self._url(path)) as r:
            if r.status_code != 200:
                raise StorageError(f"WebDAV download failed: HTTP {r.status_code}")
            async for chunk in r.aiter_bytes():
                yield chunk

    async def delete(self, path: str) -> None:
        async with self._client() as client:
            r = await client.delete(self._url(path))
        if r.status_code not in (200, 204, 404):
            raise StorageError(f"WebDAV delete failed: HTTP {r.status_code}")

    def describe(self) -> str:
        return f"WebDAV {self._url()}"


async def _iter_file(f):
    while chunk := f.read(1024 * 256):
        yield chunk
