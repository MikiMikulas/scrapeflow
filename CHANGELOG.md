# Changelog

All notable changes to ScrapeFlow. The format follows [Keep a Changelog](https://keepachangelog.com/)
and the project uses [semantic versioning](https://semver.org/).

## [0.3.0] – 2026-10-12

First public release.

### Added
- **Visual flows** per website: Website, Filter, AI filter, To Markdown (MarkItDown), Python code and
  Save to storage nodes; branches, validation, dry-run *Test on a file* and *Reprocess all files*.
- Filters run on names and links before downloading – files that can't reach storage aren't downloaded.
- Every output of a file (original, Markdown, files from code) is tracked and downloadable.
- **Local accounts**: admin setup on first start, sign-in page, 30-day sessions,
  brute-force protection, account page, personal API keys, `scrapeflow reset-password`.
- Whole-site crawler: same host, link depth and page limit, `robots.txt`, download links without an
  extension.
- Change detection with `ETag` / `Last-Modified` and SHA-256; new versions replace old ones in storage.
- Storage: local folder, S3-compatible (AWS, R2, B2, MinIO) and WebDAV (Nextcloud…), as plugins.
- Model providers: OpenAI, Anthropic, OpenRouter, Ollama, LM Studio, Groq, DeepSeek, Mistral and any
  OpenAI-compatible endpoint, as plugins.
- Schedules from hourly to monthly, computed from the last check.
- Live activity log for every check.
- Dashboard with light and dark theme, JSON API with OpenAPI docs, CLI.
- Docker image for amd64 and arm64.

[0.3.0]: https://github.com/MikiMikulas/scrapeflow/releases/tag/v0.3.0
