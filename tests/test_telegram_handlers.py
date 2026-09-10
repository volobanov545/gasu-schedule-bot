from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from szs_hub.domain.schedule import Lesson, ScheduleSnapshot
from szs_hub.schedule.coordinator import ScheduleAccess
from szs_hub.schedule.service import (
    ConfirmedSchedule,
    ScheduleFreshness,
    ScheduleFreshnessState,
)
from szs_hub.schedule.ui import ScheduleView
from szs_hub.telegram.handlers import render_schedule_view


def _access(*, stale: bool) -> ScheduleAccess:
    fetched = datetime(2026, 8, 31, 18, tzinfo=UTC)
    lesson = Lesson(
        day=date(2026, 9, 1),
        starts_at=datetime.min.time().replace(hour=9),
        ends_at=datetime.min.time().replace(hour=10, minute=30),
        subject="Геодезия",
    )
    freshness = ScheduleFreshness(
        ScheduleFreshnessState.STALE if stale else ScheduleFreshnessState.FRESH,
        fetched + timedelta(hours=1),
        fetched,
        timedelta(hours=1),
        timedelta(minutes=30),
    )
    confirmed = ConfirmedSchedule(
        1,
        ScheduleSnapshot("test", "SZS", fetched, (lesson,)),
        date(2026, 8, 31),
        None,
        freshness,
    )
    return ScheduleAccess(confirmed, False, True)


def test_schedule_view_never_hides_staleness() -> None:
    text = render_schedule_view(
        _access(stale=True),
        view=ScheduleView.TOMORROW,
        today=date(2026, 8, 31),
    )
    assert "Геодезия" in text
    assert "Последнее подтверждение" in text


def test_fresh_schedule_view_has_no_technical_noise() -> None:
    text = render_schedule_view(
        _access(stale=False),
        view=ScheduleView.TOMORROW,
        today=date(2026, 8, 31),
    )
    assert "Источник" not in text
