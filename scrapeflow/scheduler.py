"""Checks every active site when it is due.

A single job runs every minute and starts the checks whose interval has passed since the last
check. Because "due" is computed from the database, restarts don't reset weekly or monthly
schedules.
"""

import asyncio
import logging
from datetime import UTC, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlmodel import Session, select

from .db import get_engine
from .models import SCHEDULES, Site, utcnow

log = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()
_tasks: dict[int, asyncio.Task] = {}


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)  # SQLite returns naive datetimes


def next_run(site: Site) -> datetime | None:
    """When the site is due next (UTC), or None for paused / manual sites."""
    interval = SCHEDULES.get(site.schedule, ("", None))[1]
    if not site.active or interval is None:
        return None
    if site.last_checked_at is None:
        return utcnow()
    return _aware(site.last_checked_at) + interval


def start_check(site_id: int, reprocess: bool = False) -> None:
    """Run a check in the background (no-op if the site is already being checked)."""
    from .checker import claim, run_claimed

    loop = asyncio.get_running_loop()  # must be called from the event loop (async endpoints, the tick)
    if not claim(site_id):
        return
    task = loop.create_task(run_claimed(site_id, reprocess))
    _tasks[site_id] = task
    task.add_done_callback(lambda _: _tasks.pop(site_id, None))


async def cancel_check(site_id: int) -> None:
    """Stop a running background check and wait until it has cleaned up."""
    task = _tasks.get(site_id)
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def tick() -> None:
    from .checker import is_running

    now = utcnow()
    with Session(get_engine()) as session:
        sites = session.exec(select(Site).where(Site.active == True)).all()  # noqa: E712
    for site in sites:
        due = next_run(site)
        if due is not None and due <= now and not is_running(site.id):
            log.info("Checking %s (%s)", site.name, site.schedule)
            start_check(site.id)


def start() -> None:
    scheduler.add_job(tick, "interval", minutes=1, id="tick", replace_existing=True,
                      max_instances=1, coalesce=True, next_run_time=datetime.now(UTC))
    scheduler.start()
    log.info("Scheduler started")


def shutdown() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
