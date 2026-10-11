"""Walk a website and collect links to downloadable files.

Pages on the same host (``www.`` ignored) are followed breadth-first up to ``max_depth`` clicks
from the start page and ``max_pages`` pages in total. A link counts as a file when its path ends
with one of the wanted extensions (on any host – files often live on a CDN), or when a link
without a known extension turns out not to be an HTML page (``/download?id=42``).
"""

import asyncio
import logging
import re
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from urllib.parse import urldefrag, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

from .files import filename_from_headers, filename_from_url, url_extension

log = logging.getLogger(__name__)

# Extensions that are web pages, not files.
PAGE_EXTENSIONS = {"", "html", "htm", "xhtml", "shtml", "php", "php3", "asp", "aspx", "jsp", "jspx", "cfm",
                   "cgi", "pl", "do", "action"}
_SKIP_SCHEMES = ("#", "mailto:", "tel:", "javascript:", "data:", "ftp:")
_MAX_PAGE_BYTES = 5 * 1024 * 1024
# Links past the depth limit are still requested when they look like downloads without an extension.
_DOWNLOADISH = re.compile(r"download|attachment|getfile|file[_-]?id|soubor|priloha|příloha|stahn|dokument|blob",
                          re.I)


class CrawlError(RuntimeError):
    pass


@dataclass
class FileLink:
    url: str
    text: str
    page_url: str
    name: str | None = None  # from Content-Disposition, when the link was probed

    @property
    def filename(self) -> str:
        return self.name or filename_from_url(self.url)


@dataclass
class CrawlResult:
    files: list[FileLink] = field(default_factory=list)
    pages: int = 0
    errors: list[str] = field(default_factory=list)


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _key(url: str) -> str:
    """Identity of a URL for de-duplication: ``www.`` and host case don't matter."""
    p = urlparse(url)
    return p._replace(netloc=p.netloc.lower().removeprefix("www.")).geturl()


def _is_html(response: httpx.Response) -> bool:
    ctype = response.headers.get("content-type", "").split(";")[0].strip().lower()
    return ctype in ("", "text/html", "application/xhtml+xml")


async def _robots(client: httpx.AsyncClient, start: str) -> RobotFileParser | None:
    parsed = urlparse(start)
    try:
        r = await client.get(f"{parsed.scheme}://{parsed.netloc}/robots.txt", timeout=10)
    except httpx.HTTPError:
        return None
    if r.status_code != 200 or not _is_text(r):
        return None
    parser = RobotFileParser()
    parser.parse(r.text.splitlines())
    return parser


def _is_text(r: httpx.Response) -> bool:
    return r.headers.get("content-type", "text/plain").startswith("text/")


def page_links(html: str, page_url: str) -> list[tuple[str, str]]:
    """(absolute URL, link text) for every ``<a href>`` on a page, without fragments."""
    soup = BeautifulSoup(html, "html.parser")
    base_tag = soup.find("base", href=True)
    base = urljoin(page_url, base_tag["href"]) if base_tag else page_url
    links = []
    for a in soup.select("a[href]"):
        href = a["href"].strip()
        if not href or href.lower().startswith(_SKIP_SCHEMES):
            continue
        url = urldefrag(urljoin(base, href))[0]
        if url.startswith(("http://", "https://")):
            links.append((url, " ".join(a.get_text(" ").split())[:200]))
    return links


async def crawl(
    client: httpx.AsyncClient,
    start_url: str,
    *,
    extensions: set[str],
    max_depth: int = 3,
    max_pages: int = 300,
    delay: float = 0.0,
    respect_robots: bool = True,
    on_progress: Callable[[int, int], Awaitable[None]] | None = None,
    say: Callable[..., None] = lambda message, level="info": None,
) -> CrawlResult:
    """Crawl ``start_url``. ``on_progress(pages, files)`` is awaited after every page and
    ``say(message, level)`` receives a human-readable activity log."""
    host = _host(start_url)
    robots = await _robots(client, start_url) if respect_robots else None
    if respect_robots:
        say("robots.txt found – pages it disallows are skipped" if robots else "No robots.txt – all pages allowed",
            "muted")
    user_agent = client.headers.get("user-agent", "*")

    result = CrawlResult()
    files: dict[str, FileLink] = {}
    queued: set[str] = {_key(start_url)}
    # (url, depth, link text, page it was linked from); depth > max_depth = only check if it's a file
    queue: deque[tuple[str, int, str, str | None]] = deque([(start_url, 0, "", None)])

    def add_file(link: FileLink) -> None:
        files.setdefault(_key(link.url), link)

    while queue and result.pages < max_pages:
        url, depth, text, parent = queue.popleft()
        if robots and parent is not None and not robots.can_fetch(user_agent, url):
            say(f"Skipped (disallowed by robots.txt): {url}", "muted")
            continue
        if result.pages and delay:
            await asyncio.sleep(delay)
        try:
            async with client.stream("GET", url) as r:
                if r.status_code >= 400:
                    raise CrawlError(f"HTTP {r.status_code}")
                if not _is_html(r):
                    # A link without a file extension that serves a file.
                    if parent is not None:
                        name = filename_from_headers(r.headers)
                        ext = (name or "").rpartition(".")[2].lower() if name and "." in name else ""
                        if not ext or ext in extensions:
                            add_file(FileLink(url, text, parent, name))
                            say(f"Download link: {url} → {name or 'file'}", "ok")
                        continue
                    raise CrawlError("The start URL is not an HTML page")
                if depth > max_depth:
                    continue  # a page beyond the depth limit – it was only checked for being a file
                body = bytearray()
                async for chunk in r.aiter_bytes():
                    body += chunk
                    if len(body) > _MAX_PAGE_BYTES:
                        break
                final_url = str(r.url)
                html = body.decode(r.encoding or "utf-8", errors="replace")
        except (httpx.HTTPError, CrawlError) as e:
            if parent is None:
                raise CrawlError(f"Could not load {url}: {e}") from e
            result.errors.append(f"{url}: {e}")
            say(f"Page failed: {url} – {e}", "warn")
            continue

        result.pages += 1
        before = len(files)
        for link, link_text in page_links(html, final_url):
            ext = url_extension(link)
            if ext in extensions:
                add_file(FileLink(link, link_text, final_url))
            elif ext in PAGE_EXTENSIONS and _key(link) not in queued and _host(link) == host:
                if depth < max_depth or _DOWNLOADISH.search(link):
                    queued.add(_key(link))
                    queue.append((link, depth + 1, link_text, final_url))
        found = len(files) - before
        say(f"Page {result.pages}/{max_pages} · depth {depth} · {final_url}"
            + (f" → {found} new file link(s)" if found else ""), "ok" if found else "info")
        if on_progress:
            await on_progress(result.pages, len(files))

    result.files = list(files.values())
    say(f"Crawl finished: {result.pages} page(s), {len(result.files)} file link(s)"
        + (f", {len(result.errors)} page(s) failed" if result.errors else ""))
    if queue and result.pages >= max_pages:
        say(f"Stopped at the page limit ({max_pages}) with {len(queue)} link(s) not visited – "
            "raise Max pages or start from a more specific page", "warn")
    log.info("Crawled %s: %d page(s), %d file(s)", start_url, result.pages, len(result.files))
    return result
