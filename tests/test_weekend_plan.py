"""The one-off weekend joke is presentation, never university schedule data."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from html import unescape

import pytest

from szs_hub.domain.schedule import Lesson, ScheduleSnapshot, diff_schedules
from szs_hub.schedule.calendar import render_icalendar
from szs_hub.schedule.ci import (
    ScheduleEnvelope,
    decode_schedule_envelope,
    due_reminder,
    encode_schedule_envelope,
    overlapping_changes,
    render_digest_fallback,
    render_rich_digest,
)

MSK = timezone(timedelta(hours=3), "Europe/Moscow")
PLAN_DAY = date(2026, 11, 1)
ACTIVITIES = (
    ("09:00–10:00", "Поздравить мир с моим пробуждением"),
    ("12:00–13:00", "Изысканно отобедать"),
    ("14:00–17:00", "Просто сиять"),
    ("18:00–19:00", "Вечернее унижение завистников"),
    ("20:00–23:00", "Царственно отдыхать"),
)


def _lesson(day: date, *, subject: str = "Геодезия") -> Lesson:
    return Lesson(day, time(10), time(11, 30), subject, source_id=f"official:{day}")


def _envelope(*lessons: Lesson, monday: date = date(2026, 10, 26)) -> ScheduleEnvelope:
    return ScheduleEnvelope(
        group_key="3-СУЗСс-3",
        fetched_at=datetime(2026, 10, 29, 22, 55, tzinfo=MSK),
        horizon_start=monday,
        horizon_end=monday + timedelta(days=13),
        lessons=lessons,
    )


def _assert_ordinary_activity_rows(rendered: str) -> None:
    text = " ".join(unescape(re.sub(r"<[^>]*>", " ", rendered)).split())
    assert "Шуточный план" not in text
    assert "Не расписание" not in text
    for span, activity in ACTIVITIES:
        for clock in span.split("–"):
            assert clock in text
        assert text.count(activity) == 1


def test_next_free_weekend_uses_an_ordinary_sunday_disclosure_after_saturday_classes() -> None:
    # Saturday is a teaching day, so the one-off plan belongs to Sunday instead.
    official = _lesson(date(2026, 10, 31), subject="Официальная субботняя пара")
    rendered = render_rich_digest(
        _envelope(official), local_now=datetime(2026, 10, 29, 22, 55, tzinfo=MSK),
    )

    assert "Официальная субботняя пара" in rendered
    assert "<details><summary>Вс, 1 ноября</summary>" in rendered
    assert "<summary>Остальные дни недели</summary>" in rendered
    _assert_ordinary_activity_rows(rendered)
    text = " ".join(unescape(re.sub(r"<[^>]*>", " ", rendered)).split())
    assert text.index("Официальная субботняя пара") < text.index(ACTIVITIES[0][1])


@pytest.mark.parametrize("renderer", [render_rich_digest, render_digest_fallback])
def test_sunday_activities_are_the_normal_primary_day_in_both_formats(renderer) -> None:
    rendered = renderer(_envelope(), local_now=datetime.combine(PLAN_DAY, time(8), MSK))

    assert "1 ноября" in rendered
    _assert_ordinary_activity_rows(rendered)
    if renderer is render_rich_digest:
        assert "<h2>Сегодня · Воскресенье, 1 ноября</h2>" in rendered
        assert "<b>Следующая · 09:00</b>" in rendered
        assert "<details" not in rendered
    else:
        assert rendered.startswith("📅 <b>Сегодня</b> · воскресенье, 1 ноября")


def test_fallback_before_sunday_keeps_the_normal_primary_teaching_day_only() -> None:
    official = _lesson(date(2026, 10, 31), subject="Официальная субботняя пара")
    rendered = render_digest_fallback(
        _envelope(official), local_now=datetime(2026, 10, 29, 22, 55, tzinfo=MSK),
    )

    assert "Официальная субботняя пара" in rendered
    assert "31 октября" in rendered
    assert "Шуточный план" not in rendered
    assert all(activity not in rendered for _, activity in ACTIVITIES)


@pytest.mark.parametrize("renderer", [render_rich_digest, render_digest_fallback])
@pytest.mark.parametrize(
    "today,monday,busy",
    [
        (date(2026, 11, 2), date(2026, 10, 26), False),  # Expired, still in horizon.
        (date(2026, 11, 1), date(2026, 10, 12), False),  # Beyond horizon end.
        (date(2026, 10, 29), date(2026, 11, 2), False),  # Before horizon start.
        (date(2026, 10, 29), date(2026, 10, 26), True),  # Real classes take priority.
    ],
)
def test_joke_is_hidden_when_expired_outside_horizon_or_not_a_free_day(
    renderer, today: date, monday: date, busy: bool,
) -> None:
    data = _envelope(*([_lesson(PLAN_DAY)] if busy else []), monday=monday)
    rendered = renderer(data, local_now=datetime.combine(today, time(8), MSK))

    assert "Шуточный план" not in rendered
    assert all(activity not in rendered for _, activity in ACTIVITIES)


def test_rendering_joke_does_not_mutate_envelope_calendar_or_semantic_diff() -> None:
    official = _lesson(date(2026, 10, 31))
    data = _envelope(official)
    encoded_before = encode_schedule_envelope(data)
    calendar_before = render_icalendar(data)
    snapshot = ScheduleSnapshot("spbgasu", data.group_key, data.fetched_at, data.lessons)

    for renderer in (render_rich_digest, render_digest_fallback):
        for day in (date(2026, 10, 29), PLAN_DAY):
            rendered = renderer(
                data, local_now=datetime.combine(day, time(8), MSK),
            )
            assert "Шуточный план" not in rendered
            if day == PLAN_DAY:
                _assert_ordinary_activity_rows(rendered)

    assert data.lessons == (official,)
    assert encode_schedule_envelope(data) == encoded_before
    assert decode_schedule_envelope(encoded_before) == data
    assert render_icalendar(data) == calendar_before
    assert calendar_before.count("BEGIN:VEVENT") == 1
    assert "20261101T" not in calendar_before
    assert all(activity not in calendar_before for _, activity in ACTIVITIES)
    assert overlapping_changes(data, replace(data), today=date(2026, 10, 29)) == ()
    current = replace(snapshot, lessons=data.lessons)
    assert diff_schedules(snapshot, current) == ()
    assert snapshot.content_hash == current.content_hash


def test_joke_never_creates_a_class_reminder() -> None:
    data = _envelope(_lesson(date(2026, 10, 31)))

    # Include all activity boundaries and their two-hour reminder windows.
    for quarter in range(24 * 4):
        now = datetime.combine(PLAN_DAY, time(quarter // 4, quarter % 4 * 15), MSK)
        assert due_reminder(data, local_now=now, sent_markers=()) is None


@pytest.mark.parametrize("renderer", [render_rich_digest, render_digest_fallback])
def test_ordinary_earlier_week_stays_an_official_schedule_only(renderer) -> None:
    data = _envelope(_lesson(date(2026, 10, 6)), monday=date(2026, 9, 28))
    rendered = renderer(data, local_now=datetime(2026, 10, 4, 12, tzinfo=MSK))

    assert "Геодезия" in rendered
    assert "6 октября" in rendered
    assert "Шуточный план" not in rendered
    assert all(activity not in rendered for _, activity in ACTIVITIES)
