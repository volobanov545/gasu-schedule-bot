from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from szs_hub.schedule.coordinator import ScheduleCoordinator
from szs_hub.schedule.service import ScheduleSyncService
from szs_hub.schedule.spbgasu import (
    SchedulePageBootstrap,
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


class FakeClient:
    def __init__(self) -> None:
        self.bootstrap_calls = 0
        self.fail_bootstrap = False

    async def fetch_bootstrap(self) -> SchedulePageBootstrap:
        self.bootstrap_calls += 1
        if self.fail_bootstrap:
            raise SpbGasuUnavailableError("offline")
        return SchedulePageBootstrap(5, ("СЗС-3",))

    async def fetch_group(self, group_key: str) -> WeeklySchedule:
        return WeeklySchedule(
            group_key,
            (
                WeeklyLesson(
                    weekday=0,
                    slot=1,
                    parity=WeekParity.NUMERATOR,
                    subject="Геодезия",
                    group=group_key,
                    auditorium="312",
                    professor="Иванов",
                    source_date=None,
                ),
            ),
        )


@pytest.mark.asyncio
async def test_coordinator_uses_official_parity_and_fail_stale_cache(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite+aiosqlite:///{(tmp_path / 'schedule.db').as_posix()}")
    sessions = create_session_factory(engine)
    await create_schema(engine)
    now = datetime(2026, 8, 31, 12, tzinfo=UTC)
    client = FakeClient()
    service = ScheduleSyncService(client=client, session_factory=sessions, clock=lambda: now)
    coordinator = ScheduleCoordinator(
        group_key="СЗС-3",
        bootstrap_client=client,
        sync_service=service,
        clock=lambda: now,
    )
    try:
        first = await coordinator.get_week(date(2026, 8, 31))
        assert first.schedule is not None
        assert first.schedule.parity is WeekParity.NUMERATOR
        assert first.schedule.snapshot.lessons[0].subject == "Геодезия"

        client.fail_bootstrap = True
        stale = await coordinator.get_week(date(2026, 8, 31), force_refresh=True)
        assert stale.schedule is not None
        assert stale.bootstrap_available is False
        assert stale.schedule.snapshot.content_hash == first.schedule.snapshot.content_hash
    finally:
        await engine.dispose()
