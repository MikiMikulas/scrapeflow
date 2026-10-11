# Quick start

This guide takes you from nothing to a website whose files land in your storage – about five minutes.

## 1. Start ScrapeFlow

### Docker Compose (recommended)

```bash
git clone https://github.com/MikiMikulas/scrapeflow.git
cd scrapeflow
docker compose up -d
```

### Prebuilt image

```bash
docker run -d --name scrapeflow -p 8000:8000 -v scrapeflow-data:/data \
  --add-host host.docker.internal:host-gateway \
  ghcr.io/mikimikulas/scrapeflow:latest
```

Images are published for `linux/amd64` and `linux/arm64` (Raspberry Pi 4/5, Apple silicon).

### From source (Python 3.11+)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[s3]"
scrapeflow serve            # http://localhost:8000
```

## 2. Create the admin account

Open <http://localhost:8000>. On the first start every page leads to **Set up your admin account**.
Enter a name, e-mail and a password (at least 8 characters). You are signed in right away; from now
on the sign-in page is shown instead.

> Forgot the password? Run `scrapeflow reset-password you@example.com` on the server
> (`docker compose exec scrapeflow scrapeflow reset-password you@example.com`).

## 3. Connect storage

**Storage** in the top bar → pick a card (*Local folder*, *Amazon S3*, *Cloudflare R2*, *Backblaze B2*,
*MinIO*, *Nextcloud*, *WebDAV*) → fill in the details → **Test** → **Save & use**.

Without changes ScrapeFlow saves into `/data/files` inside the Docker volume. To get the files into a
folder on your computer, uncomment the two `downloads` lines in `docker-compose.yml` and restart.

More: [Storage](storage.md).

## 4. Connect a model (optional)

A model is only needed for the **AI filter** node. **Model providers** → pick a card → paste the API key
(local servers such as Ollama need none) → **Test** → **Save**.

More: [Model providers](models.md).

## 5. Add a website

**Websites** → **Add website** → paste the address of the page that lists the documents (for example
`example.com/notice-board/`) and choose how often to check. Under *Advanced* you can set the link
depth, the maximum number of pages and the file types.

The first check starts immediately. Open the website and switch to **Activity** to watch the live log.

## 6. Shape the flow

Open **Flow** in the website's menu. The default flow stores every file as it is. Drag in
**To Markdown** and a second **Save to storage** to keep a Markdown copy, or a **Filter** to keep only
some types. **Test on a file** shows the result before you save; **Reprocess all files** applies the
saved flow to files you already have.

More: [Flows](flows.md).
