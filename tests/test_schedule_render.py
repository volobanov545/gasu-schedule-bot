from __future__ import annotations

from datetime import date, time

from szs_hub.domain.schedule import ChangeKind, Lesson, ScheduleChange
from szs_hub.schedule.render import render_day_card, render_schedule_changes


def lesson(**overrides: object) -> Lesson:
    data: dict[str, object] = {
        "day": date(2026, 8, 31),
        "starts_at": time(10, 0),
        "ends_at": time(11, 30),
        "subject": "Железобетонные конструкции",
        "lesson_type": "Практика",
        "teacher": "Иванов А. А.",
        "room": "407",
        "building": "1",
    }
    data.update(overrides)
    return Lesson(**data)  # type: ignore[arg-type]


def test_day_card_is_compact_and_sorted() -> None:
    later = lesson(starts_at=time(11, 40), ends_at=time(13, 10), subject="Геодезия")
    card = render_day_card(date(2026, 8, 31), [later, lesson()], relative_label="Завтра")

    assert card.startswith("📅 <b>Завтра</b> · понедельник, 31 августа")
    assert card.index("Железобетонные") < card.index("Геодезия")
    assert "2 пары · первая в 10:00 · конец в 13:10" in card


def test_empty_day_is_quiet() -> None:
    assert render_day_card(date(2026, 8, 31), [], relative_label="Завтра").endswith("Пар нет.")


def test_html_from_source_is_escaped() -> None:
    card = render_day_card(
        date(2026, 8, 31),
        [lesson(subject="<script>alert(1)</script>", room="A&B")],
        relative_label="Завтра",
    )

    assert "<script>" not in card
    assert "&lt;script&gt;" in card
    assert "A&amp;B" in card


def test_room_diff_shows_only_the_meaningful_change() -> None:
    old = lesson(room="312")
    new = lesson(room="407")
    text = render_schedule_changes(
        [ScheduleChange(ChangeKind.ROOM, old, new)], relative_label="на завтра"
    )

    assert "312 → 407" in text
    assert "Изменение на завтра" in text

