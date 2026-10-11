# FAQ and troubleshooting

**No files were found.**
Open the website's **Activity** log. Typical reasons: the documents are more clicks away than the
*Link depth*, the file types don't match (*File types* in Settings, or a Filter node), or the page builds
its links with JavaScript – ScrapeFlow reads the HTML the server sends and does not run JavaScript.
Start from the page that lists the documents.

**The crawl takes long / visits unrelated pages.**
Lower *Link depth* and *Max pages*, or start from a more specific page. The log says when the page limit
stopped the crawl.

**A page is skipped with “disallowed by robots.txt”.**
The website asked crawlers not to visit it. ScrapeFlow follows that by default
(`SCRAPEFLOW_RESPECT_ROBOTS`). Only turn it off for websites you own or have permission to crawl.

**A file is “Failed”.**
Hover the status or open the log: HTTP errors, files over `SCRAPEFLOW_MAX_FILE_MB`, storage errors or
an error in a flow node. Failed files are retried at the next check.

**To Markdown says a PDF “has no text”.**
The PDF is a scan (images only). MarkItDown doesn't do OCR; use a Python code node with an OCR library,
or choose *Pass the original on* in the node.

**The AI filter lets everything through.**
No model is connected, or the request failed – the log says which. Check **Model providers › Test**.
Inside Docker use `host.docker.internal` instead of `localhost` for model servers on your computer.

**Is a file downloaded again at every check?**
No. ScrapeFlow sends conditional requests (`If-None-Match` / `If-Modified-Since`). Servers that support
them answer *304 Not Modified*; otherwise the file is downloaded and compared by SHA-256, and stored
only if it changed.

**What happens when a file disappears from the website?**
Its stored copy stays in your storage; the file table marks it *Removed from site*.

**I deleted a website – are the files gone?**
No. Deleting a website removes it and its history from ScrapeFlow; files in storage are kept.

**Where is my data?**
In the data volume (`/data` in Docker): the SQLite database with settings, history and logs, and – with
the local storage – the files in `/data/files`.

**How do I back up ScrapeFlow?**
Back up the data volume (stop the container or copy `scrapeflow.db*` together). Files in S3/WebDAV are
already outside the container.

**Can several people use it?**
Not as separate users yet: ScrapeFlow has one administrator account, created on the first start.
Multiple users with roles are on the [roadmap](../README.md#where-its-going). Scripts can use the API key.
