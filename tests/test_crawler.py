import httpx
import respx

from scrapeflow.crawler import crawl, page_links
from scrapeflow.files import filename_from_url, parse_extensions, unique_path

SITE = "https://www.obec.test"
EXTS = parse_extensions(None)


def html(*links: str) -> httpx.Response:
    body = "".join(f'<a href="{href}">{text}</a>' for href, text in links)
    return httpx.Response(200, text=f"<html><body>{body}</body></html>", headers={"content-type": "text/html"})


def test_page_links_resolve_and_skip_non_http():
    links = page_links('<base href="/docs/"><a href="a.pdf#p=2">A</a><a href="mailto:x@y">m</a>'
                       '<a href="javascript:void(0)">j</a><a href=" ../b.html "> B  page </a>',
                       "https://x.test/index.html")
    assert links == [("https://x.test/docs/a.pdf", "A"), ("https://x.test/b.html", "B page")]


def test_filenames_and_unique_paths():
    assert unique_path("w", "a.pdf", {"w/a.pdf", "w/a-2.pdf"}) == "w/a-3.pdf"
    assert unique_path("w", "README", {"w/README"}) == "w/README-2"
    assert filename_from_url("https://x.y/dir/") == "download"
    assert filename_from_url("https://x.y/..%2F..%2Fetc%2Fpasswd") == "passwd"
    assert filename_from_url("https://cdn.test/a%20b.zip") == "a b.zip"


@respx.mock
async def test_crawl_follows_same_site_pages_and_collects_files():
    respx.get(f"{SITE}/robots.txt").mock(return_value=httpx.Response(
        200, text="User-agent: *\nDisallow: /private/", headers={"content-type": "text/plain"}))
    respx.get(f"{SITE}/").mock(return_value=html(
        ("/deska/", "Úřední deska"), ("https://obec.test/about.html", "About"), ("/private/x.html", "Private"),
        ("https://other.test/page.html", "Elsewhere"), ("/rozpocet.xlsx", "Rozpočet"),
    ))
    respx.get(f"{SITE}/deska/").mock(return_value=html(
        ("vyhlaska.pdf", "Vyhláška 1/2026"), ("https://cdn.test/plan.zip", "Plán"), ("/download?id=7", "Příloha"),
        ("2025/", "Archiv 2025"),
    ))
    respx.get("https://obec.test/about.html").mock(return_value=html(("/rozpocet.xlsx", "Rozpočet again")))
    respx.get(f"{SITE}/download?id=7").mock(return_value=httpx.Response(
        200, content=b"%PDF", headers={"content-type": "application/pdf",
                                       "content-disposition": 'attachment; filename="priloha.pdf"'}))
    deep = respx.get(f"{SITE}/deska/2025/").mock(return_value=html(("old.pdf", "Old")))
    private = respx.get(f"{SITE}/private/x.html").mock(return_value=html())

    async with httpx.AsyncClient() as client:
        result = await crawl(client, f"{SITE}/", extensions=EXTS, max_depth=1)

    assert result.pages == 3  # start, /deska/, /about.html – not /deska/2025/ (too deep) or /private/
    assert not deep.called and not private.called
    assert {(f.url, f.filename, f.page_url) for f in result.files} == {
        (f"{SITE}/rozpocet.xlsx", "rozpocet.xlsx", f"{SITE}/"),
        (f"{SITE}/deska/vyhlaska.pdf", "vyhlaska.pdf", f"{SITE}/deska/"),
        ("https://cdn.test/plan.zip", "plan.zip", f"{SITE}/deska/"),
        (f"{SITE}/download?id=7", "priloha.pdf", f"{SITE}/deska/"),
    }


@respx.mock
async def test_crawl_limits_and_file_type_filter():
    respx.get(f"{SITE}/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(f"{SITE}/").mock(return_value=html(("/a.html", "A"), ("/b.html", "B"), ("/x.pdf", "X"),
                                                 ("/y.docx", "Y")))
    respx.get(f"{SITE}/a.html").mock(return_value=html(("/z.pdf", "Z")))
    b = respx.get(f"{SITE}/b.html").mock(return_value=html())
    async with httpx.AsyncClient() as client:
        result = await crawl(client, f"{SITE}/", extensions={"pdf"}, max_pages=2)
    assert result.pages == 2 and not b.called
    assert [f.filename for f in result.files] == ["x.pdf", "z.pdf"]


@respx.mock
async def test_crawl_start_page_errors_raise_but_subpage_errors_dont():
    import pytest

    from scrapeflow.crawler import CrawlError

    respx.get(f"{SITE}/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(f"{SITE}/gone").mock(return_value=httpx.Response(404))
    async with httpx.AsyncClient() as client:
        with pytest.raises(CrawlError, match="404"):
            await crawl(client, f"{SITE}/gone", extensions=EXTS)

    respx.get(f"{SITE}/").mock(return_value=html(("/broken.html", "x"), ("/a.pdf", "A")))
    respx.get(f"{SITE}/broken.html").mock(return_value=httpx.Response(500))
    async with httpx.AsyncClient() as client:
        result = await crawl(client, f"{SITE}/", extensions=EXTS)
    assert [f.filename for f in result.files] == ["a.pdf"] and "500" in result.errors[0]
