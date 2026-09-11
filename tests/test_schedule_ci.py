from __future__ import annotations

from datetime import UTC, date, datetime, time

from szs_hub.domain.schedule import ChangeKind, Lesson
from szs_hub.schedule.ci import (
    ScheduleDeliveryState,
    ScheduleEnvelope,
    decode_schedule_envelope,
    due_reminder,
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
    )

    assert rendered.startswith("<h1>📅 Завтра")
    assert "<table striped compact>" in rendered
    assert "<details><summary>Неделя" in rendered
    assert "<tg-button-row" not in rendered
    assert "https://" not in rendered
    assert "&lt;ЖБК &amp; геодезия&gt;" in rendered


def test_nighttime_forced_digest_keeps_the_upcoming_current_day() -> None:
    envelope = _envelope(
        date(2026, 9, 7),
        _lesson(date(2026, 9, 11), subject="Пятничная пара"),
    )

    rendered = render_rich_digest(
        envelope,
        local_now=datetime(2026, 9, 11, 1, 50, tzinfo=UTC),
    )

    assert rendered.startswith("<h1>📅 Сегодня · 11 сентября</h1>")
    assert "Пятничная пара" in rendered
    assert "Неделя · 11 сентября — 17 сентября" in rendered


def test_first_class_reminder_is_due_two_hours_before_and_only_once() -> None:
    envelope = _envelope(
        date(2026, 9, 7),
        _lesson(date(2026, 9, 11), subject="Геодезия"),
    )
    now = datetime(2026, 9, 11, 8, 45, tzinfo=UTC)

    reminder = due_reminder(envelope, local_now=now, sent_markers=())

    assert reminder is not None
    marker, text = reminder
    assert marker == "first:2026-09-11:10:45:00"
    assert "Первая пара через 2 часа" in text
    assert "10:45" in text
    assert "Геодезия" in text
    assert due_reminder(envelope, local_now=now, sent_markers=(marker,)) is None


def test_next_class_reminder_names_time_place_and_wait() -> None:
    current = _lesson(date(2026, 9, 11), subject="Сопромат")
    current = Lesson(
        day=current.day,
        starts_at=time(9, 0),
        ends_at=time(10, 30),
        subject=current.subject,
        room="312",
        building="1",
        source_id="current",
    )
    following = Lesson(
        day=current.day,
        starts_at=time(10, 45),
        ends_at=time(12, 15),
        subject="Геодезия",
        room="407",
        building="2",
        source_id="next",
    )
    envelope = _envelope(date(2026, 9, 7), current, following)

    reminder = due_reminder(
        envelope,
        local_now=datetime(2026, 9, 11, 10, 15, tzinfo=UTC),
        sent_markers=(),
    )

    assert reminder is not None
    _, text = reminder
    assert "Следующая пара через 30 мин" in text
    assert "Текущая закончится через 15 мин" in text
    assert "10:45" in text
    assert "407 · <i>корп. 2</i>" in text


def test_no_transition_reminder_after_last_class() -> None:
    envelope = _envelope(
        date(2026, 9, 7),
        Lesson(
            day=date(2026, 9, 11),
            starts_at=time(9, 0),
            ends_at=time(10, 30),
            subject="Последняя пара",
            source_id="last",
        ),
    )

    assert (
        due_reminder(
            envelope,
            local_now=datetime(2026, 9, 11, 10, 15, tzinfo=UTC),
            sent_markers=(),
        )
        is None
    )


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
