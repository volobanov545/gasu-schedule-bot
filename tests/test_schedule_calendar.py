from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, time

from szs_hub.domain.schedule import Lesson
from szs_hub.schedule.calendar import render_icalendar
from szs_hub.schedule.ci import ScheduleEnvelope


def _envelope(lesson: Lesson) -> ScheduleEnvelope:
    return ScheduleEnvelope(
        group_key="3-СУЗСс-3",
        fetched_at=datetime(2026, 9, 14, 9, 20, tzinfo=UTC),
        horizon_start=date(2026, 9, 14),
        horizon_end=date(2026, 9, 27),
        lessons=(lesson,),
    )


def _lesson() -> Lesson:
    return Lesson(
        day=date(2026, 9, 14),
        starts_at=time(9, 0),
        ends_at=time(10, 30),
        subject="Сопротивление материалов, часть 1",
        lesson_type="Практика",
        teacher="Иванов Иван Иванович",
        room="402",
        building="1",
        subgroup="1 подгруппа",
        source_id="spbgasu:stable-lesson",
    )


def test_calendar_contains_complete_read_only_lesson() -> None:
    rendered = render_icalendar(_envelope(_lesson()))

    assert rendered.startswith("BEGIN:VCALENDAR\r\n")
    assert "METHOD:PUBLISH" in rendered
    assert "BEGIN:VTIMEZONE" in rendered
    assert "TZID:Europe/Moscow" in rendered
    assert "X-WR-CALNAME:Пятница · 3-СУЗСс-3" in rendered
    assert "DTSTART;TZID=Europe/Moscow:20260914T090000" in rendered
    assert "DTEND;TZID=Europe/Moscow:20260914T103000" in rendered
    assert "Преподаватель: Иванов Иван Иванович" in rendered.replace("\r\n ", "")
    assert "LOCATION:ауд. 402 · корпус 1" in rendered.replace("\r\n ", "")
    assert "BEGIN:VALARM" not in rendered
    assert rendered.endswith("END:VCALENDAR\r\n")


def test_calendar_uid_survives_time_room_and_teacher_changes() -> None:
    original = render_icalendar(_envelope(_lesson()))
    changed_lesson = replace(
        _lesson(),
        starts_at=time(10, 45),
        ends_at=time(12, 15),
        room="407",
        teacher="Петров Пётр Петрович",
    )
    changed = render_icalendar(
        replace(
            _envelope(changed_lesson),
            fetched_at=datetime(2026, 9, 14, 10, 20, tzinfo=UTC),
        )
    )

    def uid(value: str) -> str:
        return next(line for line in value.splitlines() if line.startswith("UID:"))

    assert uid(original) == uid(changed)
    assert "DTSTART;TZID=Europe/Moscow:20260914T104500" in changed
    assert "SEQUENCE:1789381200" in changed


def test_calendar_folds_long_utf8_lines_to_75_octets() -> None:
    lesson = replace(_lesson(), subject="Очень длинное название дисциплины " * 8)
    rendered = render_icalendar(_envelope(lesson))

    for physical_line in rendered.split("\r\n"):
        assert len(physical_line.encode("utf-8")) <= 75
