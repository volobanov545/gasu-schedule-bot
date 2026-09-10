"""Consistent SQLite backup and recoverable restore helpers."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.engine import make_url

from szs_hub.storage.process_lock import ProcessLock, database_lock_path


@dataclass(frozen=True, slots=True)
class RestoreResult:
    restored_path: Path
    safety_copy: Path | None


def sqlite_database_path(database_url: str) -> Path:
    """Resolve a file-backed SQLite URL and reject memory/URI databases."""

    url = make_url(database_url)
    if not url.drivername.startswith("sqlite+"):
        raise ValueError("backup supports only SQLite databases")
    database = url.database
    if not database or database == ":memory:" or database.startswith("file:"):
        raise ValueError("backup requires a file-backed SQLite database")
    return Path(database).expanduser().resolve()


def create_backup(database_url: str, destination: Path) -> Path:
    """Create and verify an online SQLite backup, then publish it atomically."""

    source = sqlite_database_path(database_url)
    target = destination.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if target == source:
        raise ValueError("backup destination must differ from the live database")
    target.parent.mkdir(parents=True, exist_ok=True)

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with closing(
            sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)
        ) as source_db, closing(
            sqlite3.connect(temporary)
        ) as target_db:
            source_db.backup(target_db)
        verify_backup(temporary)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def verify_backup(path: Path) -> None:
    """Raise if SQLite cannot prove the copied database internally consistent."""

    candidate = path.expanduser().resolve()
    if not candidate.is_file():
        raise FileNotFoundError(candidate)
    with closing(
        sqlite3.connect(f"{candidate.as_uri()}?mode=ro", uri=True)
    ) as database:
        result = database.execute("PRAGMA quick_check").fetchone()
    if result != ("ok",):
        raise ValueError("SQLite backup integrity check failed")


def restore_backup(
    backup: Path,
    database_url: str,
    *,
    confirmed_offline: bool,
) -> RestoreResult:
    """Restore only after explicit confirmation, preserving the prior DB beside it."""

    if not confirmed_offline:
        raise PermissionError("restore requires confirmation that SZS Hub is stopped")
    source = backup.expanduser().resolve()
    target = sqlite_database_path(database_url)
    verify_backup(source)
    if source == target:
        raise ValueError("backup and live database paths must differ")
    target.parent.mkdir(parents=True, exist_ok=True)

    with ProcessLock(database_lock_path(target)):
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        safety_copy = (
            target.with_name(f"{target.name}.pre-restore-{stamp}")
            if target.exists()
            else None
        )
        if safety_copy is not None:
            create_backup(database_url, safety_copy)

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".restore", dir=target.parent
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            with closing(sqlite3.connect(source)) as source_db, closing(
                sqlite3.connect(temporary)
            ) as target_db:
                source_db.backup(target_db)
            verify_backup(temporary)
            _preserve_sidecars(target, stamp=stamp)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return RestoreResult(target, safety_copy)


def _preserve_sidecars(database: Path, *, stamp: str) -> None:
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{database}{suffix}")
        if not sidecar.exists():
            continue
        preserved = database.with_name(f"{database.name}.pre-restore-{stamp}{suffix}")
        os.replace(sidecar, preserved)
