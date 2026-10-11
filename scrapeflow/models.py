from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(UTC)


class Schedule(StrEnum):
    MANUAL = "manual"
    HOURLY = "hourly"
    SIX_HOURS = "6h"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


SCHEDULES: dict[str, tuple[str, timedelta | None]] = {
    Schedule.MANUAL: ("Manually only", None),
    Schedule.HOURLY: ("Every hour", timedelta(hours=1)),
    Schedule.SIX_HOURS: ("Every 6 hours", timedelta(hours=6)),
    Schedule.DAILY: ("Once a day", timedelta(days=1)),
    Schedule.WEEKLY: ("Once a week", timedelta(weeks=1)),
    Schedule.MONTHLY: ("Once a month", timedelta(days=30)),
}


class Site(SQLModel, table=True):
    """A website whose files are mirrored into the configured storage."""

    __table_args__ = {"sqlite_autoincrement": True}  # never reuse ids of deleted sites

    id: int | None = Field(default=None, primary_key=True)
    name: str
    url: str
    schedule: Schedule = Schedule.WEEKLY
    active: bool = True

    # Crawling
    max_depth: int = 3  # how many clicks away from the start page to follow links
    max_pages: int = 300  # stop crawling after this many pages
    file_types: str | None = None  # comma separated extensions, e.g. "pdf,docx"; None = common documents

    # Storage
    storage_folder: str | None = None  # folder in the storage; defaults to a slug of the name
    download_existing: bool = True  # also store files that are already on the site at the first check

    # What happens to each file: JSON graph edited on the Flow tab (None = Website → Save to storage)
    flow: str | None = None
    ai_filter: str | None = None  # older setting; becomes an AI filter node in the default flow

    last_checked_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class SiteFile(SQLModel, table=True):
    """A file linked from a site – and, once stored, where its latest version lives."""

    __tablename__ = "site_file"
    __table_args__ = {"sqlite_autoincrement": True}

    id: int | None = Field(default=None, primary_key=True)
    site_id: int = Field(foreign_key="site.id", index=True)
    url: str = Field(index=True)
    filename: str
    link_text: str | None = None
    page_url: str | None = None  # page the link was found on
    on_site: bool = True  # False once the link disappears from the site (the stored copy is kept)

    # stored | failed | ignored (AI filter) | skipped (already there at the first check)
    status: str = "failed"
    error: str | None = None

    size: int | None = None
    sha256: str | None = None
    content_type: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    storage_backend: str | None = None
    storage_path: str | None = None
    version: int = 0  # 1 after the first download, +1 for every changed version stored

    first_seen: datetime = Field(default_factory=utcnow, index=True)
    last_seen: datetime = Field(default_factory=utcnow)
    updated_at: datetime | None = None  # when the latest version was stored


class FileOutput(SQLModel, table=True):
    """A file a flow saved to storage – the original or something made from it (e.g. Markdown)."""

    __tablename__ = "file_output"
    __table_args__ = {"sqlite_autoincrement": True}

    id: int | None = Field(default=None, primary_key=True)
    site_file_id: int = Field(foreign_key="site_file.id", index=True)
    node_id: str  # the storage node that saved it
    filename: str
    size: int | None = None
    sha256: str | None = None
    content_type: str | None = None
    storage_backend: str
    storage_path: str
    updated_at: datetime = Field(default_factory=utcnow)


class CheckRun(SQLModel, table=True):
    """One crawl of a site."""

    __tablename__ = "check_run"
    __table_args__ = {"sqlite_autoincrement": True}

    id: int | None = Field(default=None, primary_key=True)
    site_id: int = Field(foreign_key="site.id", index=True)
    started_at: datetime = Field(default_factory=utcnow, index=True)
    finished_at: datetime | None = None
    status: str = "running"  # running | ok | error
    pages: int = 0
    files_found: int = 0
    new_files: int = 0
    updated_files: int = 0
    failed: int = 0
    error: str | None = None
    log: str | None = None  # JSON list of [time, level, message], see runlog.py


class Setting(SQLModel, table=True):
    """Key/value settings edited from the dashboard (override environment defaults)."""

    key: str = Field(primary_key=True)
    value: str = ""


class User(SQLModel, table=True):
    """A local account. The first one is created on the setup page."""

    __tablename__ = "user_account"
    __table_args__ = {"sqlite_autoincrement": True}

    id: int | None = Field(default=None, primary_key=True)
    email: str = Field(index=True, unique=True)
    name: str
    password_hash: str
    api_key_hash: str | None = Field(default=None, index=True)
    api_key_hint: str | None = None  # last 4 characters, to recognise the key
    created_at: datetime = Field(default_factory=utcnow)
    last_login_at: datetime | None = None


class AuthSession(SQLModel, table=True):
    """A signed-in browser. Only a hash of the cookie value is stored."""

    __tablename__ = "auth_session"

    token_hash: str = Field(primary_key=True)
    user_id: int = Field(foreign_key="user_account.id", index=True)
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime
