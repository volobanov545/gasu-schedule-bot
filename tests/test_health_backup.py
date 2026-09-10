from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from szs_hub.backup import create_backup, restore_backup, verify_backup
from szs_hub.health import HealthState, build_health_report
from szs_hub.storage.database import (
    create_database_engine,
    create_schema,
    create_session_factory,
)
from szs_hub.storage.models import SystemSetting
from szs_hub.storage.process_lock import ProcessLock, database_lock_path


@pytest.mark.asyncio
async def test_health_reports_missing_expected_schedule_as_degraded(tmp_path: Path) -> None:
    path = tmp_path / "health.db"
    engine = create_database_engine(f"sqlite+aiosqlite:///{path.as_posix()}")
    sessions = create_session_factory(engine)
    await create_schema(engine)
    try:
        report = await build_health_report(
            sessions,
            expect_schedule=True,
            schedule_stale_after=timedelta(hours=6),
            now=datetime(2026, 9, 1, 12, tzinfo=UTC),
        )
        assert report.state is HealthState.DEGRADED
        assert report.reasons == ("schedule_missing",)
        assert report.as_dict()["state"] == "degraded"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_health_checks_runtime_heartbeat_and_backup_age(tmp_path: Path) -> None:
    path = tmp_path / "health-runtime.db"
    status = tmp_path / "last-success"
    status.write_text("2026-09-01T10:00:00+00:00\n", encoding="utf-8")
    engine = create_database_engine(f"sqlite+aiosqlite:///{path.as_posix()}")
    sessions = create_session_factory(engine)
    await create_schema(engine)
    try:
        async with sessions() as session, session.begin():
            session.add(
                SystemSetting(
                    key="runtime.heartbeat",
                    value={"at": "2026-09-01T11:59:00+00:00"},
                )
            )
        report = await build_health_report(
            sessions,
            expect_schedule=False,
            schedule_stale_after=timedelta(hours=6),
            expect_runtime=True,
            runtime_stale_after=timedelta(minutes=5),
            backup_status_path=status,
            backup_stale_after=timedelta(hours=36),
            now=datetime(2026, 9, 1, 12, tzinfo=UTC),
        )
        assert report.state is HealthState.OK
        assert report.last_runtime_heartbeat is not None
        assert report.last_backup_success is not None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_health_fails_closed_after_telegram_rights_probe_failure(tmp_path: Path) -> None:
    path = tmp_path / "health-telegram.db"
    engine = create_database_engine(f"sqlite+aiosqlite:///{path.as_posix()}")
    sessions = create_session_factory(engine)
    await create_schema(engine)
    try:
        async with sessions() as session, session.begin():
            session.add(
                SystemSetting(
                    key="telegram.probe",
                    value={"at": "2026-09-01T11:59:00+00:00", "ok": False},
                )
            )
        report = await build_health_report(
            sessions,
            expect_schedule=False,
            schedule_stale_after=timedelta(hours=6),
            expect_telegram=True,
            now=datetime(2026, 9, 1, 12, tzinfo=UTC),
        )
        assert report.state is HealthState.FAIL
        assert report.reasons == ("telegram_probe_failed",)
        assert report.telegram_probe_ok is False
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_health_fails_without_schema_and_never_leaks_exception_text(tmp_path: Path) -> None:
    path = tmp_path / "empty.db"
    engine = create_database_engine(f"sqlite+aiosqlite:///{path.as_posix()}")
    sessions = create_session_factory(engine)
    try:
        report = await build_health_report(
            sessions,
            expect_schedule=False,
            schedule_stale_after=timedelta(hours=6),
        )
        assert report.state is HealthState.FAIL
        assert report.reasons[0].startswith("database:")
        assert str(path) not in " ".join(report.reasons)
    finally:
        await engine.dispose()


def test_backup_and_recoverable_restore(tmp_path: Path) -> None:
    live = tmp_path / "live.db"
    backup = tmp_path / "backups" / "snapshot.db"
    url = f"sqlite+aiosqlite:///{live.as_posix()}"
    with closing(sqlite3.connect(live)) as database:
        database.execute("CREATE TABLE value (content TEXT NOT NULL)")
        database.execute("INSERT INTO value VALUES ('before')")
        database.commit()
    assert create_backup(url, backup) == backup.resolve()
    verify_backup(backup)

    with closing(sqlite3.connect(live)) as database:
        database.execute("UPDATE value SET content = 'after'")
        database.commit()
    with pytest.raises(PermissionError):
        restore_backup(backup, url, confirmed_offline=False)

    runtime_lock = ProcessLock(database_lock_path(live))
    runtime_lock.acquire()
    try:
        with pytest.raises(RuntimeError, match="runtime lock"):
            restore_backup(backup, url, confirmed_offline=True)
    finally:
        runtime_lock.release()

    result = restore_backup(backup, url, confirmed_offline=True)
    assert result.safety_copy is not None and result.safety_copy.is_file()
    with closing(sqlite3.connect(live)) as database:
        assert database.execute("SELECT content FROM value").fetchone() == ("before",)
    with closing(sqlite3.connect(result.safety_copy)) as database:
        assert database.execute("SELECT content FROM value").fetchone() == ("after",)
