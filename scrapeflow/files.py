"""Downloading files: naming, size limits and conditional requests for change detection."""

import hashlib
import mimetypes
import re
import tempfile
import unicodedata
from dataclasses import dataclass
from email.message import Message
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlparse

import httpx

from .config import get_settings

DEFAULT_EXTENSIONS = (
    "pdf,doc,docx,odt,rtf,txt,xls,xlsx,ods,csv,ppt,pptx,odp,zip,rar,7z,tar,gz,tgz,xml,json,"
    "epub,mp3,mp4,jpg,jpeg,png,gif,svg,dwg,dxf,iso,exe,msi,dmg,apk,isdoc,zfo,p7s"
)


def parse_extensions(raw: str | None) -> set[str]:
    raw = raw or DEFAULT_EXTENSIONS
    return {e.strip().lower().lstrip(".") for e in re.split(r"[,\s;]+", raw) if e.strip()}


def url_extension(url: str) -> str:
    return PurePosixPath(unquote(urlparse(url).path)).suffix.lower().lstrip(".")


def filename_from_url(url: str) -> str:
    path = unquote(urlparse(url).path)
    name = "" if path.endswith("/") else PurePosixPath(path).name
    return safe_filename(name or "download")


def filename_from_headers(headers: httpx.Headers) -> str | None:
    cd = headers.get("content-disposition")
    if not cd:
        return None
    msg = Message()
    msg["content-disposition"] = cd
    name = msg.get_filename()
    return safe_filename(name) if name else None


def safe_filename(name: str) -> str:
    name = name.replace("\\", "/").split("/")[-1]
    name = re.sub(r"[\x00-\x1f<>:\"|?*]+", "_", name).strip(" .")
    return name[:180] or "download"


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    return re.sub(r"[\s_-]+", "-", text)[:60] or "files"


def unique_path(folder: str, filename: str, taken: set[str]) -> str:
    """``folder/filename``, adding ``-2``, ``-3``… when that path is already used."""
    stem, dot, ext = filename.rpartition(".")
    if not dot:
        stem, ext = filename, ""
    candidate = f"{folder}/{filename}"
    n = 2
    while candidate in taken:
        candidate = f"{folder}/{stem}-{n}{'.' + ext if ext else ''}"
        n += 1
    return candidate


@dataclass
class Downloaded:
    path: Path
    filename: str
    size: int
    sha256: str
    content_type: str | None
    etag: str | None
    last_modified: str | None


async def download(
    client: httpx.AsyncClient,
    url: str,
    *,
    referer: str | None = None,
    etag: str | None = None,
    last_modified: str | None = None,
) -> Downloaded | None:
    """Stream a file to a temporary path, enforcing the size limit.

    With ``etag`` / ``last_modified`` the request is conditional and ``None`` means the server
    answered *304 Not Modified*.
    """
    s = get_settings()
    limit = s.max_file_mb * 1024 * 1024
    headers = {}
    if referer:
        headers["Referer"] = referer
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    tmp = Path(tempfile.mkstemp(prefix="scrapeflow-", dir=s.data_dir)[1])
    digest = hashlib.sha256()
    size = 0
    try:
        async with client.stream("GET", url, headers=headers, timeout=s.request_timeout * 4) as r:
            if r.status_code == 304:
                tmp.unlink(missing_ok=True)
                return None
            if r.status_code >= 400:
                raise httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
            declared = int(r.headers.get("content-length") or 0)
            if declared > limit:
                raise ValueError(f"File is {declared // 1048576} MB, limit is {s.max_file_mb} MB")
            with tmp.open("wb") as f:
                async for chunk in r.aiter_bytes():
                    size += len(chunk)
                    if size > limit:
                        raise ValueError(f"File exceeds the {s.max_file_mb} MB limit")
                    digest.update(chunk)
                    f.write(chunk)
            content_type = (r.headers.get("content-type") or "").split(";")[0].strip() or None
            filename = filename_from_headers(r.headers) or filename_from_url(str(r.url))
            new_etag, new_modified = r.headers.get("etag"), r.headers.get("last-modified")
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    if "." not in filename and content_type:
        filename += mimetypes.guess_extension(content_type) or ""
    return Downloaded(tmp, filename, size, digest.hexdigest(), content_type, new_etag, new_modified)
