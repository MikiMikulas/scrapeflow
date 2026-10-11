import logging
from collections.abc import Iterator

from sqlalchemy import event, inspect, text
from sqlmodel import Session, SQLModel, create_engine

from .config import get_settings

log = logging.getLogger(__name__)

_engine = None


def get_engine():
    global _engine
    if _engine is None:
        url = get_settings().db_url
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        _engine = create_engine(url, connect_args=connect_args)
        if url.startswith("sqlite"):
            event.listen(_engine, "connect", _sqlite_pragmas)
    return _engine


def _sqlite_pragmas(dbapi_conn, _record) -> None:
    # WAL lets the dashboard read while a check writes; busy_timeout waits instead of failing.
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=15000")
    cur.close()


def init_db() -> None:
    from . import models  # noqa: F401  (register tables)

    engine = get_engine()
    SQLModel.metadata.create_all(engine)
    _add_missing_columns(engine)


def _add_missing_columns(engine) -> None:
    """Tiny forward-only migration: add columns that newer versions introduced.

    create_all() creates missing tables but never alters existing ones, so databases from
    older releases would miss new fields. New fields are always nullable or have a default.
    """
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table in SQLModel.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                col_type = column.type.compile(dialect=engine.dialect)
                default = column.default.arg if column.default is not None else None
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {col_type}'
                if isinstance(default, bool):
                    ddl += f" NOT NULL DEFAULT {int(default)}"
                elif isinstance(default, int | float):
                    ddl += f" NOT NULL DEFAULT {default}"
                elif isinstance(default, str):
                    ddl += " NOT NULL DEFAULT '" + default.replace("'", "''") + "'"
                conn.execute(text(ddl))
                log.info("Added column %s.%s", table.name, column.name)


def get_session() -> Iterator[Session]:
    with Session(get_engine()) as session:
        yield session
