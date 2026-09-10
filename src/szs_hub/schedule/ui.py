"""Telegram-native schedule navigation and compact secondary cards."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import date, datetime
from enum import StrEnum
from html import escape

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from szs_hub.domain.schedule import Lesson


class ScheduleView(StrEnum):
    TODAY = "today"
    TOMORROW = "tomorrow"
    WEEK = "week"


_CALLBACK_PREFIX = "schedule:v1:"
_SHORT_WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
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


def schedule_keyboard(*, active: ScheduleView) -> InlineKeyboardMarkup:
    """Three obvious views; no command memorization is required."""

    labels = {
        ScheduleView.TODAY: "Сегодня",
        ScheduleView.TOMORROW: "Завтра",
        ScheduleView.WEEK: "Неделя",
    }
    buttons = [
        InlineKeyboardButton(
            text=(f"• {label}" if view is active else label),
            callback_data=f"{_CALLBACK_PREFIX}{view.value}",
        )
        for view, label in labels.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


def parse_schedule_callback(value: str | None) -> ScheduleView | None:
    if value is None or not value.startswith(_CALLBACK_PREFIX):
        return None
    try:
        return ScheduleView(value.removeprefix(_CALLBACK_PREFIX))
    except ValueError:
        return None


def render_week_card(*, monday: date, lessons: Iterable[Lesson]) -> str:
    """Render only days with lessons, keeping a whole week readable on a phone."""

    grouped: dict[date, list[Lesson]] = defaultdict(list)
    for lesson in lessons:
        if monday <= lesson.day <= monday.fromordinal(monday.toordinal() + 6):
            grouped[lesson.day].append(lesson)

    sunday = monday.fromordinal(monday.toordinal() + 6)
    heading = (
        f"🗓 <b>Неделя · {monday.day} {_MONTHS[monday.month]} — "
        f"{sunday.day} {_MONTHS[sunday.month]}</b>"
    )
    if not grouped:
        return f"{heading}\n\nПар нет."

    blocks = [heading]
    for day in sorted(grouped):
        rows = [f"<b>{_SHORT_WEEKDAYS[day.weekday()]}, {day.day} {_MONTHS[day.month]}</b>"]
        for lesson in sorted(grouped[day], key=lambda item: (item.starts_at, item.subject)):
            location = f" · {escape(lesson.room)}" if lesson.room else ""
            rows.append(f"{lesson.starts_at:%H:%M}  {escape(lesson.subject)}{location}")
        blocks.append("\n".join(rows))
    return "\n\n".join(blocks)


def render_attendance_card(*, lesson: Lesson, starts_at: datetime, reaction: str) -> str:
    """Render the one small message posted exactly at lesson start."""

    details = [value.strip() for value in (lesson.lesson_type, lesson.teacher) if value]
    location = f"ауд. {escape(lesson.room.strip())}" if lesson.room else "аудитория не указана"
    lines = [
        f"<b>{starts_at:%H:%M} · {escape(lesson.subject.strip())}</b>",
        " · ".join(escape(item) for item in details + [location]),
        f"{escape(reaction)} — я на паре",
    ]
    return "\n\n".join(line for line in lines if line)
