# Configuration

Models, storage and websites are configured in the dashboard. Everything else is set with
environment variables – in `docker-compose.yml`, a `.env` file next to it (see
[`.env.example`](../.env.example)) or the shell.

## Server

| Variable | Default | Description |
|----------|---------|-------------|
| `SCRAPEFLOW_DATA_DIR` | `data` (`/data` in Docker) | SQLite database and temporary files |
| `SCRAPEFLOW_DATABASE_URL` | SQLite in the data dir | any SQLAlchemy URL |
| `SCRAPEFLOW_HOST` / `SCRAPEFLOW_PORT` | `0.0.0.0` / `8000` | where the web server listens |
| `SCRAPEFLOW_SCHEDULER_ENABLED` | `true` | `false` = checks only via *Check now*, the API or the CLI |

## Sign-in

| Variable | Default | Description |
|----------|---------|-------------|
| `SCRAPEFLOW_AUTH_DISABLED` | `false` | turn sign-in off – only behind your own authenticating proxy |
| `SCRAPEFLOW_SECURE_COOKIES` | auto | mark the session cookie *Secure*; auto = when the request came over HTTPS |

## Crawling

| Variable | Default | Description |
|----------|---------|-------------|
| `SCRAPEFLOW_CRAWL_DELAY` | `0.5` | seconds between page requests |
| `SCRAPEFLOW_RESPECT_ROBOTS` | `true` | follow each website's `robots.txt` |
| `SCRAPEFLOW_MAX_PARALLEL_CHECKS` | `2` | websites checked at the same time |
| `SCRAPEFLOW_REQUEST_TIMEOUT` | `30` | seconds per page request (downloads get 4×) |
| `SCRAPEFLOW_USER_AGENT` | Chrome-like + `ScrapeFlow/<version>` | User-Agent header |

## Files

| Variable | Default | Description |
|----------|---------|-------------|
| `SCRAPEFLOW_FILES_DIR` | `<data dir>/files` | default folder of the *Local folder* storage |
| `SCRAPEFLOW_MAX_FILE_MB` | `500` | larger files are not downloaded (marked as failed) |
| `SCRAPEFLOW_MAX_DOWNLOADS_PER_CHECK` | `1000` | new + changed files per check; the rest follow next time |

## Model defaults

| Variable | Description |
|----------|-------------|
| `SCRAPEFLOW_AI_PROVIDER` | `openai` (any OpenAI-compatible API) or `anthropic`, or a plugin name |
| `SCRAPEFLOW_AI_BASE_URL` | e.g. `http://host.docker.internal:11434/v1` |
| `SCRAPEFLOW_AI_MODEL` | e.g. `qwen2.5:7b` |
| `SCRAPEFLOW_AI_API_KEY` | falls back to `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` |
| `SCRAPEFLOW_AI_EFFORT` | Anthropic only, default `low` |
| `SCRAPEFLOW_AI_MAX_CHARS` | longest prompt sent to the model, default `20000` |
| `SCRAPEFLOW_AI_TIMEOUT` | seconds, default `120` |

Values saved on the *Model providers* page take precedence.

## Website settings

Set per website in the dashboard (*Add website* or the website's *Settings* tab):

| Setting | Description |
|---------|-------------|
| Website | start page; ScrapeFlow follows links on the same host (`www.` ignored) |
| Name | defaults to the domain |
| Check | manually, every hour, every 6 hours, daily, weekly or monthly – counted from the last check, so restarts don't reset it |
| Also store files already on the website | only when adding: otherwise the first check records existing files without downloading them |
| Link depth | clicks away from the start page; `0` = only the start page |
| Max pages | pages visited per check |
| File types | which links count as files, e.g. `pdf, docx, xlsx`; empty = common documents, spreadsheets, archives, images and media. Links without an extension are recognised by the server's response. |
| Folder in storage | the `{site}` folder of storage nodes; defaults to a slug of the name |
| Check automatically | untick to pause the schedule |

Everything that happens after a file is found is the website's [flow](flows.md).
