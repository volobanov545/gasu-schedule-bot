"""Compact, Telegram-safe Russian schedule presentation."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from html import escape

from szs_hub.domain.schedule import ChangeKind, Lesson, ScheduleChange

_WEEKDAYS = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)
_MONTHS = (
    "",
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


def render_day_card(day: date, lessons: Iterable[Lesson], *, relative_label: str) -> str:
    ordered = sorted(lessons, key=lambda lesson: (lesson.starts_at, lesson.subject.casefold()))
    heading = f"📅 <b>{escape(relative_label)}</b> · {_date_label(day)}"
    if not ordered:
        return f"{heading}\n\nПар нет."

    blocks = [heading]
    for lesson in ordered:
        lines = [
            f"<b>{_time_range(lesson)}</b>",
            escape(lesson.subject.strip()),
        ]
        details = _lesson_details(lesson)
        if details:
            lines.append(details)
        location = _location(lesson)
        if location:
            lines.append(location)
        blocks.append("\n".join(lines))

    count = len(ordered)
    summary = (
        f"{count} {_russian_pair_word(count)} · "
        f"первая в {ordered[0].starts_at:%H:%M} · "
        f"конец в {ordered[-1].ends_at:%H:%M}"
    )
    blocks.append(summary)
    return "\n\n".join(blocks)


def render_schedule_changes(changes: Iterable[ScheduleChange], *, relative_label: str) -> str:
    ordered = list(changes)
    if not ordered:
        return ""
    heading = f"⚠️ <b>Изменение {escape(relative_label.casefold())}</b>"
    rows = [heading]
    for change in ordered:
        rows.append(_render_change(change))
    return "\n\n".join(rows)


def _render_change(change: ScheduleChange) -> str:
    old = change.before
    new = change.after
    lesson = new or old
    assert lesson is not None
    label = f"{escape(lesson.subject)} · {lesson.starts_at:%H:%M}"

    if change.kind is ChangeKind.ADDED:
        assert new is not None
        return f"➕ {label}\nДобавлена · {_location(new) or 'аудитория не указана'}"
    if change.kind is ChangeKind.CANCELLED:
        return f"Отмена · {label}"
    assert old is not None and new is not None

    if change.kind is ChangeKind.TIME:
        return f"{escape(new.subject)}\n{_time_range(old)} → {_time_range(new)}"
    if change.kind is ChangeKind.ROOM:
        return f"{label}\n{_safe_value(old.room)} → {_safe_value(new.room)}"
    if change.kind is ChangeKind.BUILDING:
        return f"{label}\nКорпус: {_safe_value(old.building)} → {_safe_value(new.building)}"
    if change.kind is ChangeKind.TEACHER:
        return f"{label}\n{_safe_value(old.teacher)} → {_safe_value(new.teacher)}"
    if change.kind is ChangeKind.LESSON_TYPE:
        return f"{label}\n{_safe_value(old.lesson_type)} → {_safe_value(new.lesson_type)}"
    return f"{label}\n{escape(old.subject)} → {escape(new.subject)}"


def _date_label(day: date) -> str:
    return f"{_WEEKDAYS[day.weekday()]}, {day.day} {_MONTHS[day.month]}"


def _time_range(lesson: Lesson) -> str:
    return f"{lesson.starts_at:%H:%M}–{lesson.ends_at:%H:%M}"


def _lesson_details(lesson: Lesson) -> str:
    parts = [
        escape(value.strip())
        for value in (lesson.lesson_type, lesson.teacher)
        if value and value.strip()
    ]
    return " · ".join(parts)


def _location(lesson: Lesson) -> str:
    room = escape(lesson.room.strip()) if lesson.room and lesson.room.strip() else ""
    building = (
        escape(lesson.building.strip()) if lesson.building and lesson.building.strip() else ""
    )
    if room and building:
        return f"ауд. {room} · корпус {building}"
    if room:
        return f"ауд. {room}"
    if building:
        return f"корпус {building}"
    return ""


def _safe_value(value: str | None) -> str:
    return escape(value.strip()) if value and value.strip() else "не указано"


def _russian_pair_word(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return "пара"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return "пары"
    return "пар"

