from __future__ import annotations

from datetime import UTC, date, datetime, time

from szs_hub.domain.schedule import Lesson
from szs_hub.schedule.ui import (
    ScheduleView,
    parse_schedule_callback,
    render_attendance_card,
    render_week_card,
    schedule_keyboard,
)


def _lesson(day: date, *, subject: str = "Геодезия", room: str = "312") -> Lesson:
    return Lesson(
        day=day,
        starts_at=time(10),
        ends_at=time(11, 30),
        subject=subject,
        lesson_type="Практика",
        teacher="Иванов А. А.",
        room=room,
    )


def test_schedule_keyboard_has_three_self_explanatory_views() -> None:
    keyboard = schedule_keyboard(active=ScheduleView.TOMORROW)
    buttons = keyboard.inline_keyboard[0]
    assert [button.text for button in buttons] == ["Сегодня", "• Завтра", "Неделя"]
    assert parse_schedule_callback(buttons[0].callback_data) is ScheduleView.TODAY
    assert parse_schedule_callback("schedule:v1:unknown") is None


def test_week_card_is_compact_sorted_and_html_safe() -> None:
    monday = date(2026, 8, 31)
    card = render_week_card(
        monday=monday,
        lessons=[
            _lesson(date(2026, 9, 2), subject="<ЖБК>", room="A&B"),
            _lesson(monday),
        ],
    )
    assert card.index("Пн") < card.index("Ср")
    assert "&lt;ЖБК&gt;" in card
    assert "A&amp;B" in card


def test_attendance_card_has_only_actionable_information() -> None:
    lesson = _lesson(date(2026, 8, 31), subject="ЖБК")
    text = render_attendance_card(
        lesson=lesson,
        starts_at=datetime(2026, 8, 31, 10, tzinfo=UTC),
        reaction="❤",
    )
    assert "10:00 · ЖБК" in text
    assert "❤ — я на паре" in text
    assert "законч" not in text
