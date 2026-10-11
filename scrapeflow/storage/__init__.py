"""Where downloaded files go. Backends are plugins (``scrapeflow.storage`` entry points)."""

import logging
from functools import lru_cache
from importlib.metadata import entry_points

from sqlmodel import Session, select

from ..db import get_engine
from ..models import Setting
from .backends import LocalStorage, S3Storage, WebDAVStorage
from .base import ConfigField, StorageBackend, StorageError

__all__ = ["ConfigField", "LocalStorage", "StorageBackend", "StorageError", "all_options", "backends",
           "get_storage", "load_options", "save_options"]

log = logging.getLogger(__name__)


@lru_cache
def backends() -> dict[str, type[StorageBackend]]:
    found: dict[str, type[StorageBackend]] = {
        LocalStorage.name: LocalStorage,
        S3Storage.name: S3Storage,
        WebDAVStorage.name: WebDAVStorage,
    }
    for ep in entry_points(group="scrapeflow.storage"):
        try:
            cls = ep.load()
            if isinstance(cls, type) and issubclass(cls, StorageBackend):
                found[cls.name or ep.name] = cls
        except Exception:
            log.exception("Failed to load storage plugin %s", ep.name)
    return found


def _rows(session: Session | None) -> dict[str, str]:
    own = session is None
    session = session or Session(get_engine())
    try:
        rows = session.exec(select(Setting).where(Setting.key.startswith("storage_"))).all()
    finally:
        if own:
            session.close()
    return {r.key.removeprefix("storage_"): r.value for r in rows}


def all_options(session: Session | None = None) -> tuple[str, dict[str, dict[str, str]]]:
    """(active backend, {backend: {field: value}}) – options are kept per backend."""
    values = _rows(session)
    active = values.pop("backend", "") or "local"
    per_backend: dict[str, dict[str, str]] = {}
    for key, value in values.items():
        backend, _, field = key.partition(".")
        if field:
            per_backend.setdefault(backend, {})[field] = value
    return active, per_backend


def load_options(session: Session | None = None) -> tuple[str, dict[str, str]]:
    """(active backend, its options) as saved on the Settings page; defaults to a local folder."""
    active, per_backend = all_options(session)
    return active, per_backend.get(active, {})


def save_options(session: Session, backend: str, options: dict[str, str]) -> None:
    def put(key: str, value: str) -> None:
        row = session.get(Setting, f"storage_{key}") or Setting(key=f"storage_{key}")
        row.value = value
        session.add(row)

    put("backend", backend)
    for field, value in options.items():
        put(f"{backend}.{field}", value)
    session.commit()


def get_storage(backend: str | None = None, options: dict[str, str] | None = None) -> StorageBackend:
    """The configured backend; explicit arguments override saved values (used by "Test")."""
    active, per_backend = all_options()
    backend = backend or active
    options = {**per_backend.get(backend, {}), **(options or {})}
    cls = backends().get(backend)
    if cls is None:
        raise StorageError(f"Unknown storage backend {backend!r}")
    return cls(options)
