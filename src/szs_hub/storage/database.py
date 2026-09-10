"""Async engine/session setup and guarded SQLite pragmas."""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from enum import StrEnum
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from szs_hub.storage.base import Base

SQLiteVersion = tuple[int, int, int]

# Upstream safety boundary: https://sqlite.org/wal.html#the_wal_reset_bug


class UnsafeSQLiteVersionError(RuntimeError):
    """Raised when deployment would enable WAL on a vulnerable SQLite runtime."""


class SQLiteJournalMode(StrEnum):
    WAL = "WAL"
    DELETE = "DELETE"


def sqlite_wal_is_safe(
    version: SQLiteVersion,
    *,
    backport_confirmed: bool = False,
) -> bool:
    """Return whether WAL is safe from SQLite's WAL-reset corruption bug.

    The upstream fix is in 3.51.3. SQLite also published fixed backport lines at
    3.44.6 and 3.50.7. A distributor may backport the patch without changing the
    SQLite version; callers must opt in explicitly after confirming that patch.
    """

    if version < (3, 7, 0):
        return False
    if backport_confirmed:
        return True
    if version >= (3, 51, 3):
        return True
    branch = version[:2]
    return (branch == (3, 44) and version[2] >= 6) or (branch == (3, 50) and version[2] >= 7)


def select_sqlite_journal_mode(
    version: SQLiteVersion,
    *,
    environment: str,
    backport_confirmed: bool = False,
) -> SQLiteJournalMode:
    """Select WAL only for a safe runtime, failing closed outside local use."""

    normalized_environment = environment.casefold()
    if normalized_environment not in {"local", "staging", "production"}:
        raise ValueError(f"unknown application environment: {environment}")
    if sqlite_wal_is_safe(version, backport_confirmed=backport_confirmed):
        return SQLiteJournalMode.WAL
    if normalized_environment == "local":
        return SQLiteJournalMode.DELETE
    rendered_version = ".".join(str(part) for part in version)
    raise UnsafeSQLiteVersionError(
        "SQLite "
        f"{rendered_version} is not approved for WAL; require SQLite >= 3.51.3, "
        "a fixed 3.44.6+/3.50.7+ backport line, or an explicitly confirmed vendor "
        "backport"
    )


def create_database_engine(
    database_url: str,
    *,
    environment: str = "local",
    sqlite_wal_backport_confirmed: bool = False,
    sqlite_version: SQLiteVersion | None = None,
    echo: bool = False,
) -> AsyncEngine:
    """Build the async engine and install SQLite durability/safety pragmas."""

    url = make_url(database_url)
    if not url.drivername.startswith("sqlite+"):
        raise ValueError("the storage layer requires an async SQLite database URL")

    detected_version = sqlite_version or _runtime_sqlite_version()
    journal_mode = select_sqlite_journal_mode(
        detected_version,
        environment=environment,
        backport_confirmed=sqlite_wal_backport_confirmed,
    )
    engine = create_async_engine(
        url,
        echo=echo,
        pool_pre_ping=True,
        connect_args={"timeout": 5.0},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def configure_sqlite(dbapi_connection: Any, _connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute(f"PRAGMA journal_mode={journal_mode.value}")
            if journal_mode is SQLiteJournalMode.WAL:
                cursor.execute("PRAGMA synchronous=NORMAL")
            else:
                cursor.execute("PRAGMA synchronous=FULL")
        finally:
            cursor.close()

    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Return the single session policy used by request and worker code."""

    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


@asynccontextmanager
async def session_scope(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Commit an application unit of work or roll it back on failure."""

    async with session_factory() as session, session.begin():
        yield session


async def create_schema(engine: AsyncEngine) -> None:
    """Create metadata for tests and disposable local databases.

    Persistent deployments should use Alembic instead.
    """

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        from szs_hub.storage.fts import install_search_projection

        await install_search_projection(connection)


async def drop_schema(engine: AsyncEngine) -> None:
    """Drop metadata for isolated tests only."""

    async with engine.begin() as connection:
        from szs_hub.storage.fts import uninstall_search_projection

        await uninstall_search_projection(connection)
        await connection.run_sync(Base.metadata.drop_all)


def _runtime_sqlite_version() -> SQLiteVersion:
    major, minor, patch = sqlite3.sqlite_version_info
    return major, minor, patch
