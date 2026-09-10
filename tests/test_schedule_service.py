from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import func, select

from alembic import command
from szs_hub.domain.schedule import ChangeKind
from szs_hub.schedule.service import (
    ScheduleFreshnessState,
    ScheduleSourceFailure,
    ScheduleSyncService,
)
from szs_hub.schedule.spbgasu import (
    SpbGasuUnavailableError,
    WeeklyLesson,
    WeeklySchedule,
    WeekParity,
)
from szs_hub.storage.database import (
    create_database_engine,
    create_schema,
    create_session_factory,
)
from szs_hub.storage.models import (
    Lesson,
    ScheduleChange,
    ScheduleSnapshot,
    SystemSetting,
)

MONDAY = date(2026, 8, 31)


@dataclass
class MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value


class FakeClient:
    def __init__(self, result: WeeklySchedule | Exception) -> None:
        self.result = result
        self.calls = 0

    async def fetch_group(self, group_key: str) -> WeeklySchedule:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        assert self.result.group_key == group_key
        return self.result


def _url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


def _weekly(*, room: str = "407/1") -> WeeklySchedule:
    return WeeklySchedule(
        group_key="СЗС-3",
        lessons=(
            WeeklyLesson(
                weekday=1,
                slot=1,
                parity=WeekParity.NUMERATOR,
                subject="Железобетонные конструкции (пр.)",
                group="СЗС-3",
                auditorium=room,
                professor="Иванов А. А.",
                source_date=None,
            ),
        ),
    )


@pytest.mark.asyncio
async def test_minimal_projection_does_not_persist_teacher_names(tmp_path: Path) -> None:
    engine = create_database_engine(_url(tmp_path / "minimal.sqlite3"))
    sessions = create_session_factory(engine)
    service = ScheduleSyncService(
        client=FakeClient(_weekly()),
        session_factory=sessions,
        clock=MutableClock(datetime(2026, 8, 30, 12, 0, tzinfo=UTC)),
        retain_teacher_names=False,
    )
    try:
        await create_schema(engine)
        result = await service.sync_week(
            group_key="СЗС-3", week_start=MONDAY, parity=WeekParity.NUMERATOR
        )

        assert result.schedule is not None
        assert result.schedule.snapshot.lessons[0].teacher is None
        async with sessions() as session:
            stored = await session.scalar(select(Lesson))
        assert stored is not None and stored.teacher is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_snapshot_is_immutable_idempotent_and_no_change_check_refreshes_confirmation(
    tmp_path: Path,
) -> None:
    engine = create_database_engine(_url(tmp_path / "schedule.sqlite3"))
    sessions = create_session_factory(engine)
    clock = MutableClock(datetime(2026, 8, 30, 12, 0, tzinfo=UTC))
    client = FakeClient(_weekly())
    service = ScheduleSyncService(client=client, session_factory=sessions, clock=clock)
    try:
        await create_schema(engine)
        first = await service.sync_week(
            group_key=" СЗС-3 ", week_start=MONDAY, parity=WeekParity.NUMERATOR
        )
        assert first.schedule is not None
        assert first.snapshot_created
        assert first.freshness.state is ScheduleFreshnessState.FRESH
        assert first.schedule.snapshot.fetched_at == clock.value
        first_snapshot_id = first.schedule.snapshot_id

        clock.value += timedelta(minutes=20)
        second = await service.sync_week(
            group_key="СЗС-3", week_start=MONDAY, parity=WeekParity.NUMERATOR
        )
        assert second.schedule is not None
        assert second.schedule.snapshot_id == first_snapshot_id
        assert second.schedule.snapshot.fetched_at == datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
        assert second.freshness.confirmed_at == clock.value
        assert not second.snapshot_created
        assert second.changes == ()
        assert second.change_ids == ()

        async with sessions() as db:
            snapshot_count = await db.scalar(select(func.count()).select_from(ScheduleSnapshot))
            lesson_count = await db.scalar(select(func.count()).select_from(Lesson))
            marker_count = await db.scalar(select(func.count()).select_from(SystemSetting))
        assert snapshot_count == 1
        assert lesson_count == 1
        assert marker_count == 1
        assert client.calls == 2

        clock.value += timedelta(minutes=31)
        stale = await service.load_latest_confirmed(group_key="СЗС-3", week_start=MONDAY)
        assert stale is not None
        assert stale.freshness.state is ScheduleFreshnessState.STALE
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_semantic_diff_and_change_rows_are_not_duplicated(tmp_path: Path) -> None:
    engine = create_database_engine(_url(tmp_path / "changes.sqlite3"))
    sessions = create_session_factory(engine)
    clock = MutableClock(datetime(2026, 8, 30, 12, 0, tzinfo=UTC))
    client = FakeClient(_weekly(room="407/1"))
    service = ScheduleSyncService(client=client, session_factory=sessions, clock=clock)
    try:
        await create_schema(engine)
        await service.sync_week(
            group_key="СЗС-3", week_start=MONDAY, parity=WeekParity.NUMERATOR
        )

        client.result = _weekly(room="408/1")
        clock.value += timedelta(minutes=10)
        changed = await service.sync_week(
            group_key="СЗС-3", week_start=MONDAY, parity=WeekParity.NUMERATOR
        )
        assert changed.snapshot_created
        assert [change.kind for change in changed.changes] == [ChangeKind.ROOM]
        assert len(changed.change_ids) == 1

        clock.value += timedelta(minutes=10)
        retried = await service.sync_week(
            group_key="СЗС-3", week_start=MONDAY, parity=WeekParity.NUMERATOR
        )
        assert not retried.snapshot_created
        assert retried.changes == ()
        assert retried.change_ids == ()

        async with sessions() as db:
            snapshots = await db.scalar(select(func.count()).select_from(ScheduleSnapshot))
            stored_changes = tuple(await db.scalars(select(ScheduleChange)))
        assert snapshots == 2
        assert len(stored_changes) == 1
        assert stored_changes[0].kind == ChangeKind.ROOM.value
        assert stored_changes[0].notified_at is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_failed_fetch_keeps_last_confirmation_but_marks_it_stale_durably(
    tmp_path: Path,
) -> None:
    engine = create_database_engine(_url(tmp_path / "stale.sqlite3"))
    sessions = create_session_factory(engine)
    clock = MutableClock(datetime(2026, 8, 30, 12, 0, tzinfo=UTC))
    client = FakeClient(_weekly())
    service = ScheduleSyncService(
        client=client,
        session_factory=sessions,
        clock=clock,
        fresh_for=timedelta(hours=1),
    )
    try:
        await create_schema(engine)
        initial = await service.sync_week(
            group_key="СЗС-3", week_start=MONDAY, parity=WeekParity.NUMERATOR
        )
        assert initial.schedule is not None

        client.result = SpbGasuUnavailableError("secret upstream diagnostic")
        clock.value += timedelta(minutes=5)
        failed = await service.sync_week(
            group_key="СЗС-3", week_start=MONDAY, parity=WeekParity.NUMERATOR
        )
        assert failed.schedule is not None
        assert failed.schedule.snapshot_id == initial.schedule.snapshot_id
        assert failed.freshness.state is ScheduleFreshnessState.STALE
        assert failed.freshness.source_failure is ScheduleSourceFailure.UNAVAILABLE
        assert failed.freshness.confirmed_at == initial.freshness.confirmed_at
        assert failed.changes == ()

        # A new service instance must not forget that the most recent source check failed.
        restarted = ScheduleSyncService(
            client=client,
            session_factory=sessions,
            clock=clock,
            fresh_for=timedelta(hours=1),
        )
        loaded = await restarted.load_latest_confirmed(group_key="СЗС-3", week_start=MONDAY)
        assert loaded is not None
        assert loaded.freshness.state is ScheduleFreshnessState.STALE
        assert loaded.freshness.source_failure is ScheduleSourceFailure.UNAVAILABLE

        client.result = _weekly()
        clock.value += timedelta(minutes=1)
        recovered = await restarted.sync_week(
            group_key="СЗС-3", week_start=MONDAY, parity=WeekParity.NUMERATOR
        )
        assert recovered.schedule is not None
        assert recovered.freshness.state is ScheduleFreshnessState.FRESH
        assert recovered.freshness.source_failure is None
        loaded_after_recovery = await restarted.load_latest_confirmed(
            group_key="СЗС-3", week_start=MONDAY
        )
        assert loaded_after_recovery is not None
        assert loaded_after_recovery.freshness.state is ScheduleFreshnessState.FRESH
        assert loaded_after_recovery.freshness.source_failure is None

        async with sessions() as db:
            assert await db.scalar(select(func.count()).select_from(ScheduleSnapshot)) == 1
            marker = await db.scalar(select(SystemSetting))
            assert marker is not None
            assert "secret upstream diagnostic" not in str(marker.value)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_no_confirmed_data_is_explicitly_unavailable_after_failure(tmp_path: Path) -> None:
    engine = create_database_engine(_url(tmp_path / "unavailable.sqlite3"))
    sessions = create_session_factory(engine)
    clock = MutableClock(datetime(2026, 8, 30, 12, 0, tzinfo=UTC))
    service = ScheduleSyncService(
        client=FakeClient(SpbGasuUnavailableError("offline")),
        session_factory=sessions,
        clock=clock,
    )
    try:
        await create_schema(engine)
        result = await service.sync_week(
            group_key="СЗС-3", week_start=MONDAY, parity=WeekParity.NUMERATOR
        )
        assert result.schedule is None
        assert result.freshness.state is ScheduleFreshnessState.UNAVAILABLE
        assert result.freshness.source_failure is ScheduleSourceFailure.UNAVAILABLE
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_empty_weeks_have_distinct_identity_and_load_independently(tmp_path: Path) -> None:
    engine = create_database_engine(_url(tmp_path / "empty-weeks.sqlite3"))
    sessions = create_session_factory(engine)
    clock = MutableClock(datetime(2026, 8, 30, 12, 0, tzinfo=UTC))
    service = ScheduleSyncService(
        client=FakeClient(_weekly()),
        session_factory=sessions,
        clock=clock,
    )
    next_monday = MONDAY + timedelta(days=7)
    try:
        await create_schema(engine)
        first = await service.sync_week(
            group_key="СЗС-3", week_start=MONDAY, parity=WeekParity.DENOMINATOR
        )
        clock.value += timedelta(minutes=1)
        second = await service.sync_week(
            group_key="СЗС-3", week_start=next_monday, parity=WeekParity.DENOMINATOR
        )
        assert first.schedule is not None
        assert second.schedule is not None
        assert first.schedule.snapshot.lessons == ()
        assert second.schedule.snapshot.lessons == ()
        assert first.schedule.snapshot_id != second.schedule.snapshot_id
        assert (await service.load_latest_confirmed(
            group_key="СЗС-3", week_start=MONDAY
        )).snapshot_id == first.schedule.snapshot_id  # type: ignore[union-attr]
        assert (await service.load_latest_confirmed(
            group_key="СЗС-3", week_start=next_monday
        )).snapshot_id == second.schedule.snapshot_id  # type: ignore[union-attr]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_request_and_clock_validation_fail_closed(tmp_path: Path) -> None:
    engine = create_database_engine(_url(tmp_path / "validation.sqlite3"))
    sessions = create_session_factory(engine)
    client = FakeClient(_weekly())
    aware = ScheduleSyncService(
        client=client,
        session_factory=sessions,
        clock=lambda: datetime(2026, 8, 30, 12, 0, tzinfo=UTC),
    )
    naive = ScheduleSyncService(
        client=client,
        session_factory=sessions,
        clock=lambda: datetime(2026, 8, 30, 12, 0),
    )
    try:
        await create_schema(engine)
        with pytest.raises(ValueError, match="Monday"):
            await aware.sync_week(
                group_key="СЗС-3",
                week_start=MONDAY + timedelta(days=1),
                parity=WeekParity.NUMERATOR,
            )
        with pytest.raises(ValueError, match="timezone-aware"):
            await naive.sync_week(
                group_key="СЗС-3", week_start=MONDAY, parity=WeekParity.NUMERATOR
            )
    finally:
        await engine.dispose()


def test_week_start_migration_backfills_existing_snapshot_and_replaces_identity(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "schedule-migration.sqlite3"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", _url(database_path))
    command.upgrade(config, "8e9d7f42c1a0")

    connection = sqlite3.connect(database_path)
    try:
        cursor = connection.execute(
            """
            INSERT INTO schedule_snapshots
                (source, group_key, fetched_at, content_hash, is_complete)
            VALUES (?, ?, ?, ?, 1)
            """,
            ("spbgasu", "СЗС-3", "2026-08-30 12:00:00", "a" * 64),
        )
        snapshot_id = cursor.lastrowid
        connection.execute(
            """
            INSERT INTO lessons
                (snapshot_id, lesson_key, day, starts_at, ends_at, subject)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (snapshot_id, "lesson-1", "2026-09-01", "09:00:00", "10:30:00", "ЖБК"),
        )
        connection.commit()
    finally:
        connection.close()

    command.upgrade(config, "head")

    connection = sqlite3.connect(database_path)
    try:
        week_start = connection.execute(
            "SELECT week_start FROM schedule_snapshots WHERE id = ?", (snapshot_id,)
        ).fetchone()
        columns = {
            row[1]: row[3]
            for row in connection.execute("PRAGMA table_info(schedule_snapshots)").fetchall()
        }
        indexes = {
            row[1]
            for row in connection.execute("PRAGMA index_list(schedule_snapshots)").fetchall()
        }
        assert week_start == ("2026-08-31",)
        assert columns["week_start"] == 1
        assert "ix_schedule_snapshots_group_week_fetched" in indexes

        # The same content is valid for a distinct requested week.
        connection.execute(
            """
            INSERT INTO schedule_snapshots
                (source, group_key, week_start, fetched_at, content_hash, is_complete)
            VALUES (?, ?, ?, ?, ?, 1)
            """,
            ("spbgasu", "СЗС-3", "2026-09-07", "2026-09-06 12:00:00", "a" * 64),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO schedule_snapshots
                    (source, group_key, week_start, fetched_at, content_hash, is_complete)
                VALUES (?, ?, ?, ?, ?, 1)
                """,
                ("spbgasu", "СЗС-3", "2026-08-31", "2026-09-01 12:00:00", "a" * 64),
            )
    finally:
        connection.close()
