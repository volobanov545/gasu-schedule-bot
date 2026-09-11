from __future__ import annotations

from datetime import UTC, date, datetime, time

from szs_hub.domain.schedule import ChangeKind, Lesson
from szs_hub.schedule.ci import (
    ScheduleDeliveryState,
    ScheduleEnvelope,
    decode_schedule_envelope,
    encode_schedule_envelope,
    load_delivery_state,
    overlapping_changes,
    render_rich_digest,
    save_delivery_state,
    should_publish_digest,
)


def _lesson(
    day: date,
    *,
    room: str = "312",
    subject: str = "Геодезия",
    source_id: str = "lesson-1",
) -> Lesson:
    return Lesson(
        day=day,
        starts_at=time(10, 45),
        ends_at=time(12, 15),
        subject=subject,
        lesson_type="Практика",
        room=room,
        building="1",
        source_id=source_id,
    )


def _envelope(
    monday: date,
    *lessons: Lesson,
    force_digest: bool = False,
) -> ScheduleEnvelope:
    return ScheduleEnvelope(
        group_key="3-СУЗСс-3",
        fetched_at=datetime(2026, 9, 1, 17, 20, tzinfo=UTC),
        horizon_start=monday,
        horizon_end=date.fromordinal(monday.toordinal() + 13),
        lessons=lessons,
        force_digest=force_digest,
    )


def test_envelope_round_trip_preserves_human_text() -> None:
    original = _envelope(date(2026, 8, 31), _lesson(date(2026, 9, 1)))

    restored = decode_schedule_envelope(encode_schedule_envelope(original))

    assert restored == original


def test_newly_exposed_future_week_is_not_reported_as_a_change() -> None:
    known = _lesson(date(2026, 9, 1))
    previous = _envelope(date(2026, 8, 24), known)
    current = _envelope(
        date(2026, 8, 31),
        known,
        _lesson(date(2026, 9, 8), subject="ЖБК", room="407", source_id="lesson-2"),
    )

    assert overlapping_changes(previous, current, today=date(2026, 9, 1)) == ()


def test_room_change_on_already_known_date_is_reported() -> None:
    previous = _envelope(date(2026, 8, 31), _lesson(date(2026, 9, 1), room="312"))
    current = _envelope(date(2026, 8, 31), _lesson(date(2026, 9, 1), room="407"))

    changes = overlapping_changes(previous, current, today=date(2026, 9, 1))

    assert [change.kind for change in changes] == [ChangeKind.ROOM]


def test_rich_digest_uses_article_primitives_and_escapes_source_data() -> None:
    envelope = _envelope(
        date(2026, 8, 31),
        _lesson(date(2026, 9, 2), subject="<ЖБК & геодезия>"),
    )

    rendered = render_rich_digest(
        envelope,
        local_now=datetime(2026, 9, 1, 20, 30, tzinfo=UTC),
        source_url="https://rasp.spbgasu.ru/",
    )

    assert rendered.startswith("<h1>📅 Завтра")
    assert "<table striped compact>" in rendered
    assert "<details><summary>Неделя" in rendered
    assert "<tg-button-row" in rendered
    assert "&lt;ЖБК &amp; геодезия&gt;" in rendered


def test_nighttime_forced_digest_keeps_the_upcoming_current_day() -> None:
    envelope = _envelope(
        date(2026, 9, 7),
        _lesson(date(2026, 9, 11), subject="Пятничная пара"),
    )

    rendered = render_rich_digest(
        envelope,
        local_now=datetime(2026, 9, 11, 1, 50, tzinfo=UTC),
        source_url="https://rasp.spbgasu.ru/",
    )

    assert rendered.startswith("<h1>📅 Сегодня · 11 сентября</h1>")
    assert "Пятничная пара" in rendered
    assert "Неделя · 11 сентября — 17 сентября" in rendered


def test_delivery_state_survives_ci_cache_and_digest_is_once_per_day(tmp_path) -> None:
    path = tmp_path / "state" / "schedule.json"
    envelope = _envelope(date(2026, 8, 31), force_digest=False)
    state = ScheduleDeliveryState(
        previous=envelope,
        last_digest_date=date(2026, 9, 1),
    )

    save_delivery_state(path, state)
    restored = load_delivery_state(path)

    assert restored == state
    assert not should_publish_digest(
        restored,
        envelope,
        local_now=datetime(2026, 9, 1, 20, 30, tzinfo=UTC),
    )
    assert should_publish_digest(
        restored,
        envelope,
        local_now=datetime(2026, 9, 2, 20, 30, tzinfo=UTC),
    )
