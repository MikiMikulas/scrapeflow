# Contributing to ScrapeFlow

Thanks for helping out! Bug reports, ideas and pull requests are all welcome.

## Development setup

```bash
git clone https://github.com/MikiMikulas/scrapeflow.git
cd scrapeflow
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest && ruff check .
scrapeflow serve --reload          # http://localhost:8000
```

Tests use a temporary data directory each, sign-in is turned off for them (except `tests/test_auth.py`)
and all network calls are mocked with [`respx`](https://lundberg.github.io/respx/).

## Project layout

```
scrapeflow/
  crawler.py    walks a website, collects file links (robots.txt, depth/page limits)
  checker.py    one check: crawl → flow filters → download new / changed files → flow → storage
  flow.py       flow nodes, validation and the engine (filters, Markdown, Python code)
  runlog.py     the live activity log of a check
  auth.py       local accounts, sessions, API keys
  files.py      downloads with size limit and conditional requests, file naming
  scheduler.py  starts checks when they're due
  ai.py         the AI file filter (provider-independent)
  llm/          model providers: OpenAI-compatible, Anthropic, plugin loading
  storage/      storage backends: local, S3, WebDAV, plugin loading
  presets.py    provider and storage cards shown in the dashboard
  web.py        FastAPI dashboard + JSON API
  templates/    Jinja2 templates + inline SVG icons (no JS build step)
  static/       CSS, the flow editor (flow.js) and the vendored Drawflow library
docs/           user documentation, screenshots, logo, architecture diagram
tests/          pytest suite
```

## Guidelines

- Keep the single-process, SQLite, no-build-step setup – it's what makes ScrapeFlow easy to self-host.
- Add tests for new behaviour.
- Run `ruff check .` and `pytest` before opening a pull request.
- Update the docs (`README.md`, `docs/`) when behaviour changes, and add a line to `CHANGELOG.md`.
- Never put real credentials, personal data or private URLs into code, tests or screenshots.

## Good first issues

- New flow nodes in `scrapeflow/flow.py`: chunking for RAG, webhooks, OCR for scanned PDFs, sending
  documents to RAGFlow / Dify
- Notification channels (ntfy, e-mail, Slack, webhooks) – see [docs/plugins.md](docs/plugins.md)
- Storage plugins (SFTP, Google Drive, Dropbox, OneDrive) and provider plugins (Gemini, Bedrock…)
- Import / export of websites and flows as JSON
- Per-site custom headers and cookies (pages behind a login)
- Rendering JavaScript-only pages with a headless browser, as an optional extra

## License

By contributing you agree that your contributions are licensed under the
[Apache License 2.0](LICENSE), the license of this project.
