"""One check of one site: crawl → find new and changed files → store them."""

import asyncio
import hashlib
import logging
import mimetypes

import httpx
from sqlalchemy import update
from sqlalchemy.orm.exc import ObjectDeletedError
from sqlmodel import Session, select

from . import ai, flow, runlog, storage
from .config import get_settings
from .crawler import CrawlError, FileLink, crawl
from .db import get_engine
from .files import download, parse_extensions, slugify, unique_path
from .models import CheckRun, FileOutput, Site, SiteFile, utcnow
from .notifiers import notify_all

log = logging.getLogger(__name__)

_running: set[int] = set()
_semaphore: tuple[asyncio.AbstractEventLoop, asyncio.Semaphore] | None = None


def is_running(site_id: int) -> bool:
    return site_id in _running


def _slots() -> asyncio.Semaphore:
    global _semaphore
    loop = asyncio.get_running_loop()
    if _semaphore is None or _semaphore[0] is not loop:
        _semaphore = (loop, asyncio.Semaphore(get_settings().max_parallel_checks))
    return _semaphore[1]


def mark_interrupted() -> None:
    """Checks still "running" at startup were cut off by a restart."""
    with Session(get_engine()) as session:
        session.exec(update(CheckRun).where(CheckRun.status == "running")
                     .values(status="error", error="Interrupted – ScrapeFlow was restarted", finished_at=utcnow()))
        session.commit()


def claim(site_id: int) -> bool:
    """Mark a site as being checked. False if a check is already running or queued."""
    if site_id in _running:
        return False
    _running.add(site_id)
    return True


async def run_check(site_id: int, reprocess: bool = False) -> CheckRun | None:
    """Check a site now. Returns None if it is already being checked (or doesn't exist).

    ``reprocess`` sends every stored file through the flow again, changed or not – used after
    editing the flow."""
    if not claim(site_id):
        return None
    return await run_claimed(site_id, reprocess)


async def run_claimed(site_id: int, reprocess: bool = False) -> CheckRun | None:
    try:
        async with _slots():
            return await _run(site_id, reprocess)
    finally:
        _running.discard(site_id)


class CheckError(RuntimeError):
    pass


async def _run(site_id: int, reprocess: bool = False) -> CheckRun | None:
    with Session(get_engine()) as session:
        site = session.get(Site, site_id)
        if site is None:
            return None
        run = CheckRun(site_id=site_id)
        session.add(run)
        session.commit()
        say = runlog.start(site_id, run.id)
        say(f"Check started: {site.url} · link depth {site.max_depth} · max {site.max_pages} pages · "
            f"file types: {site.file_types or 'all common documents'}")
        try:
            if reprocess:
                say("Every stored file goes through the flow again (reprocess)")
            await _sync(session, site, run, say, reprocess)
            run.status, site.last_error = "ok", None
            say(f"Done: {run.pages} pages, {run.files_found} files on the site – {run.new_files} new, "
                f"{run.updated_files} updated, {run.failed} failed", "error" if run.failed else "ok")
        except asyncio.CancelledError:  # the site was deleted (or ScrapeFlow is shutting down)
            session.rollback()
            runlog.finish(site_id)
            raise
        except (CrawlError, CheckError, storage.StorageError) as e:
            session.rollback()
            run.status = "error"
            run.error = site.last_error = str(e)
            say(f"Check failed: {e}", "error")
        except Exception as e:  # never let one broken site kill the scheduler
            session.rollback()
            log.exception("Unexpected error while checking site %s", site_id)
            run.status = "error"
            run.error = site.last_error = f"Unexpected error: {type(e).__name__}: {e}"
            say(f"Check failed – {run.error}", "error")
        runlog.finish(site_id)
        run.log = say.dump()
        run.finished_at = site.last_checked_at = utcnow()
        session.add_all([run, site])
        try:
            session.commit()
        except ObjectDeletedError:
            session.rollback()
            log.info("Site %s was deleted during its check", site_id)
            return None
        session.refresh(run)
        if run.new_files or run.updated_files:
            await notify_all(site, run)
        return run


async def _sync(session: Session, site: Site, run: CheckRun, say: runlog.RunLog, reprocess: bool = False) -> None:
    s = get_settings()
    flow_def = flow.load(site.flow, site.ai_filter)
    if problems := flow.validate(flow_def):
        raise CheckError("The flow has a problem: " + problems[0])
    for note in flow.warnings(flow_def):
        say(note, "warn")
    headers = {"User-Agent": s.user_agent, "Accept-Language": "cs,en;q=0.8"}

    async def progress(pages: int, files: int) -> None:
        if pages % 5 == 0:  # show progress in the dashboard
            run.pages, run.files_found = pages, files
            session.add(run)
            session.commit()

    async with httpx.AsyncClient(timeout=s.request_timeout, follow_redirects=True, headers=headers) as client:
        result = await crawl(
            client, site.url, extensions=parse_extensions(site.file_types), max_depth=site.max_depth,
            max_pages=site.max_pages, delay=s.crawl_delay, respect_robots=s.respect_robots,
            on_progress=progress, say=say,
        )
        run.pages, run.files_found = result.pages, len(result.files)
        if not result.files:
            say("No file links found. Try a larger link depth, other file types, or start from the page "
                "that lists the documents.", "warn")

        first_check = session.exec(
            select(CheckRun.id).where(CheckRun.site_id == site.id, CheckRun.status == "ok")
        ).first() is None
        known = {f.url: f for f in session.exec(select(SiteFile).where(SiteFile.site_id == site.id))}
        now = utcnow()
        found = {link.url for link in result.files}
        session.exec(update(SiteFile).where(SiteFile.site_id == site.id).values(on_site=False))
        for link in result.files:
            record = known.get(link.url)
            if record is not None:
                record.on_site, record.last_seen, record.page_url = True, now, link.page_url
                session.add(record)
        session.commit()

        new_links = [link for link in result.files if link.url not in known]
        gone = sum(1 for url in known if url not in found)
        say(f"{len(new_links)} new file(s), {len(found) - len(new_links)} already known"
            + (f", {gone} no longer on the site" if gone else ""))
        if first_check and not site.download_existing and new_links:
            for link in new_links:
                session.add(_record(site, link, status="skipped"))
            session.commit()
            say(f"First check: {len(new_links)} existing file(s) recorded but not downloaded "
                "(“Also store files already on the website” is off)", "muted")
            new_links = []

        retry = [f for f in known.values() if f.status == "failed" and f.url in found]
        stored = [f for f in known.values() if f.status == "stored" and f.url in found]
        if not (new_links or retry or stored):
            return

        work = flow.workdir(s.data_dir)
        try:
            job = _Job(session, site, run, say, client, flow_def, work)
            await job.execute(new_links, retry, stored, {link.url: link for link in result.files}, reprocess)
        finally:
            flow.cleanup(work)


def _record(site: Site, link: FileLink, status: str = "failed") -> SiteFile:
    return SiteFile(site_id=site.id, url=link.url, filename=link.filename, link_text=link.text or None,
                    page_url=link.page_url, status=status)


class _Job:
    """Sends the files of one check through the site's flow."""

    def __init__(self, session: Session, site: Site, run: CheckRun, say: runlog.RunLog, client, flow_def: dict,
                 work):
        self.session, self.site, self.run, self.say, self.client = session, site, run, say, client
        self.flow = flow_def
        self.runner = flow.FlowRunner(flow_def, work, say, self._ai_decide)
        self.site_folder = (site.storage_folder or "").strip("/ ") or slugify(site.name)
        self._backend = None
        self._ai_warned = False
        outputs = session.exec(select(FileOutput).join(SiteFile, SiteFile.id == FileOutput.site_file_id)
                               .where(SiteFile.site_id == site.id)).all()
        self.outputs = {(o.site_file_id, o.node_id, o.filename): o for o in outputs}
        self.taken = set(session.exec(select(FileOutput.storage_path))) | set(
            session.exec(select(SiteFile.storage_path).where(SiteFile.storage_path != None)))  # noqa: E711

    @property
    def backend(self):
        if self._backend is None:
            self._backend = storage.get_storage()
            self.say(f"Storage: {self._backend.describe()}", "muted")
        return self._backend

    async def execute(self, new_links: list[FileLink], retry: list[SiteFile], stored: list[SiteFile],
                  links: dict[str, FileLink], reprocess: bool) -> None:
        s, say = get_settings(), self.say
        self._adopt_old_outputs(stored)
        candidates = [_record(self.site, link) for link in new_links] + retry
        if any(n["type"] == "ai_filter" for n in self.flow["nodes"]) and candidates:
            say(f"Asking the model about {len(candidates)} file name(s)…")
            await self.runner.prefetch_ai([self._item(r, links[r.url]) for r in candidates])
        if retry:
            say(f"Retrying {len(retry)} file(s) that failed last time")

        budget = s.max_downloads_per_check
        for i, record in enumerate(candidates, 1):
            if budget <= 0:
                say(f"Download limit per check reached ({s.max_downloads_per_check}) – "
                    f"{len(candidates) - i + 1} file(s) follow next time", "warn")
                break
            if await self._handle(record, links[record.url], existing=False) != "skipped":
                budget -= 1

        if stored and budget > 0:
            say(f"{'Reprocessing' if reprocess else 'Checking'} {len(stored)} stored file(s)"
                + ("" if reprocess else " for changes"))
        unchanged = 0
        for record in stored:
            if budget <= 0:
                say("Download limit per check reached – the remaining files are checked next time", "warn")
                break
            outcome = await self._handle(record, links[record.url], existing=True, force=reprocess)
            if outcome != "skipped":
                budget -= 1
            if outcome in ("skipped", "unchanged"):
                unchanged += 1
        if unchanged:
            say(f"{unchanged} stored file(s) unchanged", "muted")

    def _item(self, record: SiteFile, link: FileLink) -> flow.Item:
        return flow.Item(record.filename, {"url": link.url, "link_text": link.text, "page_url": link.page_url,
                                           "content_type": record.content_type, "site": self.site.name})

    async def _ai_decide(self, instructions: str, items: list[flow.Item]) -> set[int] | None:
        if not ai.ai_available():
            if not self._ai_warned:
                self.say("An AI filter is in the flow but no model is connected – it lets every file through",
                         "warn")
                self._ai_warned = True
            return None
        links = [FileLink(i.meta.get("url") or i.filename, i.meta.get("link_text") or "",
                          i.meta.get("page_url") or "", i.filename) for i in items]
        keep = await ai.filter_files(instructions, self.site.name, links)
        if keep is None:
            self.say("AI filter failed (see the server log) – letting the files through", "warn")
            return None
        return {n for n, link in enumerate(links) if link.url in keep}

    async def _handle(self, record: SiteFile, link: FileLink, *, existing: bool, force: bool = False) -> str:
        """Plan, download and process one file.

        Returns "skipped" (filtered before downloading), "unchanged", "processed" or "failed"."""
        session, say, run = self.session, self.say, self.run
        item = self._item(record, link)
        frontier, stopped_by = await self.runner.plan(item)
        if not frontier:
            if not existing:
                record.status, record.error = "ignored", f"Filtered out by {stopped_by}"
                say(f"– {record.filename}: filtered out by {stopped_by}", "muted")
                session.add(record)
                session.commit()
            return "skipped"

        if not existing:
            say(f"Downloading {record.url}", "muted")
        try:
            dl = await download(self.client, link.url, referer=link.page_url,
                                etag=None if (force or not existing) else record.etag,
                                last_modified=None if (force or not existing) else record.last_modified)
        except Exception as e:  # network errors, size limit, HTTP errors
            error = f"Download failed: {e if str(e) else type(e).__name__}"
            if existing:
                say(f"Could not check {record.filename} for changes: {error}", "warn")
                return "failed"
            record.status, record.error = "failed", error
            run.failed += 1
            say(f"✗ {record.filename}: {error}", "error")
            session.add(record)
            session.commit()
            return "failed"
        if dl is None:  # 304 Not Modified
            return "unchanged"
        try:
            if existing and not force and dl.sha256 == record.sha256:
                record.etag, record.last_modified = dl.etag, dl.last_modified
                session.add(record)
                session.commit()
                return "unchanged"
            if record.id is None:  # commit right away: never keep a write transaction open across awaits
                session.add(record)
                session.commit()
            item.filename, item.path = dl.filename, dl.path
            item.meta["content_type"] = dl.content_type
            say(f"{'↻ Changed' if existing and not force else '↓ Got'} {dl.filename} ({runlog.filesize(dl.size)})"
                + (f" → {', '.join(self.runner.label(n) for n in frontier)}" if frontier else ""),
                "info")
            stored_paths: list[tuple[str, str]] = []

            async def store(node_id: str, out: flow.Item) -> None:
                stored_paths.append((out.filename, await self._store(record, node_id, out)))

            result = await self.runner.process(item, frontier, store)
        finally:
            dl.path.unlink(missing_ok=True)

        record.filename = dl.filename  # the server's name (Content-Disposition) beats the URL
        if stored_paths:
            primary = next((p for name, p in stored_paths if name == dl.filename), stored_paths[0][1])
            record.storage_backend, record.storage_path = self.backend.name, primary
            _apply(record, dl)
            if result.errors:
                record.error = "; ".join(result.errors)[:500]
            if existing:
                run.updated_files += 1
            else:
                run.new_files += 1
        elif result.errors:
            record.status, record.error = ("stored" if existing else "failed"), "; ".join(result.errors)[:500]
            run.failed += 1
        else:
            record.status, record.error = "ignored", "The flow produced nothing to save"
            say(f"– {dl.filename}: the flow produced nothing to save", "muted")
        session.add(record)
        session.commit()
        return "failed" if result.errors and not stored_paths else "processed"

    async def _store(self, record: SiteFile, node_id: str, out: flow.Item) -> str:
        try:
            backend = self.backend
            ext = out.filename.rpartition(".")[2].lower() if "." in out.filename else "files"
            template = (self.runner.cfg(node_id).get("folder") or "{site}").strip()
            folder = template.replace("{site}", self.site_folder).replace("{ext}", ext).strip("/ ")
            folder = folder or self.site_folder
            key = (record.id, node_id, out.filename)
            existing = self.outputs.get(key)
            if existing and existing.storage_backend == backend.name:
                path = existing.storage_path
            else:
                path = unique_path(folder, out.filename, self.taken)
                self.taken.add(path)
            content_type = out.meta.get("content_type") or mimetypes.guess_type(out.filename)[0]
            await backend.put(path, out.path, content_type)
        except storage.StorageError as e:
            raise flow.FlowError(f"storage error – {e}") from e
        size, digest = await asyncio.to_thread(_size_and_hash, out.path)
        output = existing or FileOutput(site_file_id=record.id, node_id=node_id, filename=out.filename,
                                        storage_backend=backend.name, storage_path=path)
        output.storage_backend, output.storage_path = backend.name, path
        output.size, output.sha256, output.content_type, output.updated_at = size, digest, content_type, utcnow()
        self.session.add(output)
        self.session.commit()
        self.outputs[key] = output
        self.say(f"  ✓ Saved {out.filename} ({runlog.filesize(size)}) → {path}", "ok")
        return path

    def _adopt_old_outputs(self, stored: list[SiteFile]) -> None:
        """Files stored before flows existed become outputs of the default storage node."""
        node_ids = {n["id"] for n in self.flow["nodes"] if n["type"] == "storage"}
        if "storage" not in node_ids:
            return
        have = {o.site_file_id for o in self.outputs.values()}
        for record in stored:
            if record.id not in have and record.storage_path:
                o = FileOutput(site_file_id=record.id, node_id="storage", filename=record.filename,
                               size=record.size, sha256=record.sha256, content_type=record.content_type,
                               storage_backend=record.storage_backend or "local", storage_path=record.storage_path)
                self.session.add(o)
                self.outputs[(record.id, "storage", record.filename)] = o
        self.session.commit()


def _size_and_hash(path) -> tuple[int, str]:
    digest, size = hashlib.sha256(), 0
    with open(path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def _apply(record: SiteFile, dl) -> None:
    record.status, record.error = "stored", None
    record.size, record.sha256, record.content_type = dl.size, dl.sha256, dl.content_type
    record.etag, record.last_modified = dl.etag, dl.last_modified
    record.version += 1
    record.updated_at = utcnow()
