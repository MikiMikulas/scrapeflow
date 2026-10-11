# API and CLI

## JSON API

Interactive OpenAPI docs are at <http://localhost:8000/docs> (sign in first). Scripts authenticate
with an API key from **Account & API key**:

```bash
export SF="Authorization: Bearer sf-…"

# Add a website, checked weekly, PDFs only
curl -X POST localhost:8000/api/sites -H "$SF" -H 'content-type: application/json' \
  -d '{"url": "https://example.com/notice-board/", "schedule": "weekly", "file_types": "pdf"}'

# Crawl now and wait for the result
curl -X POST localhost:8000/api/sites/1/check -H "$SF"

# Stored files
curl 'localhost:8000/api/files?site_id=1&status=stored' -H "$SF"
```

### Websites

| Method & path | Description |
|---------------|-------------|
| `GET /api/sites` | list websites |
| `POST /api/sites` | add a website (`url`, `name`, `schedule`, `max_depth`, `max_pages`, `file_types`, `storage_folder`, `download_existing`, `active`) |
| `GET /api/sites/{id}` | read |
| `PUT /api/sites/{id}` | replace the settings |
| `DELETE /api/sites/{id}` | delete the website and its history (stored files stay in storage) |
| `POST /api/sites/{id}/check` | crawl now and wait; `409` if a check is already running |
| `GET /api/sites/{id}/status` | whether a check is running, the last run |
| `GET /api/sites/{id}/runs` | check history |
| `GET /api/sites/{id}/log` | activity log of a check; `?run_id=` for an older one, `?after=<next>` to follow a running one |

`schedule` is one of `manual`, `hourly`, `6h`, `daily`, `weekly`, `monthly`.

### Flows

| Method & path | Description |
|---------------|-------------|
| `GET /api/sites/{id}/flow` | the flow (default flow if none was saved) and warnings |
| `PUT /api/sites/{id}/flow` | `{"flow": {...}}` – validated, `422` with the first problem |
| `POST /api/sites/{id}/flow/test` | `{"file_id": 12, "flow": {...}?}` – dry run: log and outputs with previews |

The flow format is described in [Flows](flows.md#flow-json).

### Files

| Method & path | Description |
|---------------|-------------|
| `GET /api/files` | files; `?site_id=`, `?status=stored\|failed\|ignored\|skipped`, `?limit=`, `?offset=` |
| `GET /files/{id}/download` | download the main stored copy of a file |
| `GET /outputs/{id}/download` | download one output of a file (e.g. its Markdown) |

### Settings

| Method & path | Description |
|---------------|-------------|
| `GET/PUT /api/settings/storage` | active storage and its options (secrets masked as `••••`) |
| `POST /api/storage/test` | `{"backend": "s3", "options": {...}}` – write, read and delete a test file |
| `GET/PUT /api/settings/ai` | model settings (the key is never returned) |
| `POST /api/ai/test` | send a tiny request with the given or saved settings |

## CLI

```bash
scrapeflow serve [--host 0.0.0.0] [--port 8000] [--reload]
scrapeflow list                              # websites
scrapeflow check 3                           # crawl website #3 now and print the result
scrapeflow reset-password you@example.com    # set a new password, sign out all browsers
```

In Docker prefix the commands with `docker compose exec scrapeflow`.
