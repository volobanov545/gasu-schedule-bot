from __future__ import annotations

from dataclasses import replace
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
    render_changes_fallback,
    render_digest_fallback,
    render_evening_summary,
    render_rich_changes,
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
    teacher: str | None = "Иванов Иван Иванович",
) -> Lesson:
    return Lesson(
        day=day,
        starts_at=time(10, 45),
        ends_at=time(12, 15),
        subject=subject,
        lesson_type="Практика",
        teacher=teacher,
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

    assert rendered.startswith("<h1>🗓 Расписание</h1><p><b>3-СУЗСс-3</b>")
    assert "<hr/>" in rendered
    assert "<b>🗓 31 авг–6 сен</b> (1 пара)" in rendered
    assert "<b>🔭 7–13 сен</b> (пар нет)" in rendered
    assert "Эта неделя" not in rendered
    assert "Следующая неделя" not in rendered
    assert "<table bordered compact>" in rendered
    assert '<td rowspan="4" align="center" valign="middle">' in rendered
    assert '<th colspan="3" align="center" valign="middle">' in rendered
    assert '<th align="center" valign="middle">Тип</th>' in rendered
    assert '<th align="center" valign="middle">Ауд.</th>' in rendered
    assert '<th align="center" valign="middle">Корп.</th>' in rendered
    assert '<td align="center" valign="middle">Практика</td>' in rendered
    assert '<td align="center" valign="middle"><b>312</b></td>' in rendered
    assert '<td align="center" valign="middle"><b>1</b></td>' in rendered
    assert '<td colspan="3" align="center" valign="middle">👤' in rendered
    assert "<th>Где</th>" not in rendered
    assert "<details" in rendered
    assert "<tg-button-row" not in rendered
    assert "https://" not in rendered
    assert "&lt;ЖБК &amp; геодезия&gt;" in rendered
    assert "Иванов Иван Иванович" in rendered


def test_rich_digest_gives_optional_subgroup_its_own_full_width_row() -> None:
    lesson = replace(_lesson(date(2026, 9, 2)), subgroup="Подгруппа 1")
    envelope = _envelope(date(2026, 8, 31), lesson)

    rendered = render_rich_digest(
        envelope,
        local_now=datetime(2026, 9, 1, 20, 30, tzinfo=UTC),
    )

    assert '<td rowspan="5" align="center" valign="middle">' in rendered
    assert "👥 <i>Подгруппа 1</i>" in rendered


def test_calendar_card_links_to_the_live_phone_feed() -> None:
    envelope = _envelope(date(2026, 9, 14), _lesson(date(2026, 9, 14)))
    url = "https://volobanov545.github.io/gasu-schedule-bot/calendar.ics"

    rich = render_rich_digest(
        envelope,
        local_now=datetime(2026, 9, 14, 8, tzinfo=UTC),
        calendar_feed_url=url,
    )
    fallback = render_digest_fallback(
        envelope,
        local_now=datetime(2026, 9, 14, 8, tzinfo=UTC),
        calendar_feed_url=url,
    )

    assert '<tg-button-row align="center">' in rich
    assert 'style="primary"' in rich
    assert "📲 Подключить календарь" in rich
    assert url in rich
    assert url in fallback


def test_current_lesson_gets_a_live_visual_accent() -> None:
    envelope = _envelope(date(2026, 9, 14), _lesson(date(2026, 9, 14)))

    rendered = render_rich_digest(
        envelope,
        local_now=datetime(2026, 9, 14, 11, tzinfo=UTC),
    )

    assert "🟢 <mark><b>Сейчас</b></mark>" in rendered
    assert "<b>Сейчас</b><br><b>10:45</b>" in rendered
    assert "<mark><b>Геодезия</b></mark>" not in rendered


def test_next_lesson_colors_only_its_time_cell() -> None:
    envelope = _envelope(date(2026, 9, 14), _lesson(date(2026, 9, 14)))

    rendered = render_rich_digest(
        envelope,
        local_now=datetime(2026, 9, 14, 8, tzinfo=UTC),
    )

    assert (
        '<th rowspan="4" align="center" valign="middle">'
        "<mark><b>Далее</b></mark><br><b>10:45</b>"
    ) in rendered
    assert "<u><b>Геодезия</b></u>" not in rendered


def test_nighttime_forced_digest_keeps_the_upcoming_current_day() -> None:
    envelope = _envelope(
        date(2026, 9, 7),
        _lesson(date(2026, 9, 11), subject="Пятничная пара"),
    )

    rendered = render_rich_digest(
        envelope,
        local_now=datetime(2026, 9, 11, 1, 50, tzinfo=UTC),
    )

    assert rendered.startswith(
        "<h1>🗓 Расписание</h1><p><b>3-СУЗСс-3</b> · <i>7–20 сентября</i></p>"
    )
    assert "Пятничная пара" in rendered
    assert "<mark>Сегодня</mark> (1 пара)" in rendered
    assert "<mark>Сегодня</mark> ·" not in rendered
    assert "<details open>" in rendered


def test_change_card_shows_delta_and_complete_current_and_previous_event() -> None:
    old = _lesson(
        date(2026, 9, 11),
        room="312",
        teacher="Иванов Иван Иванович",
    )
    new = _lesson(
        date(2026, 9, 11),
        room="407",
        teacher="Петров Пётр Петрович",
    )
    changes = overlapping_changes(
        _envelope(date(2026, 9, 7), old),
        _envelope(date(2026, 9, 7), new),
        today=date(2026, 9, 11),
    )

    rich = render_rich_changes(changes, fetched_at=datetime(2026, 9, 11, 8, tzinfo=UTC))
    fallback = render_changes_fallback(changes)

    assert rich.startswith("<h2>⚡ Расписание изменилось</h2>")
    assert "<details open>" in rich
    assert "Аудитория" in rich
    assert "Преподаватель" in rich
    assert "<s>312</s> → <mark>407</mark>" in rich
    assert "<caption><b>✨ Актуальная пара</b></caption>" in rich
    assert "Геодезия" in rich
    assert "Практика" in rich
    assert "ауд. 407, корп. 1" in rich
    assert "Петров Пётр Петрович" in rich
    assert "Прежний вариант" in rich
    assert "Иванов Иван Иванович" in rich
    assert "Где: ауд. 407, корп. 1" in fallback
    assert "Преподаватель: Петров Пётр Петрович" in fallback


def test_added_and_cancelled_change_cards_keep_the_complete_event() -> None:
    lesson = _lesson(
        date(2026, 9, 11),
        room="407",
        subject="Техническая механика",
        teacher="Петров Пётр Петрович",
    )
    empty = _envelope(date(2026, 9, 7))
    populated = _envelope(date(2026, 9, 7), lesson)

    added = render_rich_changes(
        overlapping_changes(empty, populated, today=date(2026, 9, 11)),
        fetched_at=populated.fetched_at,
    )
    cancelled = render_rich_changes(
        overlapping_changes(populated, empty, today=date(2026, 9, 11)),
        fetched_at=empty.fetched_at,
    )

    assert "<b>🆕 Добавлена</b>" in added
    assert "<caption><b>✨ Актуальная пара</b></caption>" in added
    assert "Техническая механика" in added
    assert "Петров Пётр Петрович" in added
    assert "<b>🚨 Отменена</b>" in cancelled
    assert "<caption><b>🚨 Отменённая пара</b></caption>" in cancelled
    assert "Техническая механика" in cancelled
    assert "Петров Пётр Петрович" in cancelled


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
    assert "🌅 Первая пара в 10:45" in text
    assert "<i>Через 2 часа</i>" in text
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
    assert "⏰ Следующая в 10:45" in text
    assert "<i>Через 30 мин</i>" in text
    assert "10:45" in text
    assert "407 · <i>корп. 2</i>" in text


def test_evening_summary_is_short_and_links_to_pinned_calendar() -> None:
    envelope = _envelope(
        date(2026, 9, 7),
        _lesson(date(2026, 9, 12), subject="Геодезия"),
    )

    rendered = render_evening_summary(
        envelope,
        local_now=datetime(2026, 9, 11, 20, 30, tzinfo=UTC),
        calendar_url="https://t.me/c/123/42/77",
    )

    assert rendered.startswith("<b>🌙 Завтра · 12 сентября</b>")
    assert "📚 1 пара" in rendered
    assert "1 пара · 10:45–12:15" in rendered
    assert "Открыть календарь" in rendered
    assert "Иванов" not in rendered


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


def test_reminders_hide_full_group_key_but_keep_a_real_subgroup() -> None:
    full_group_lesson = Lesson(
        day=date(2026, 9, 11),
        starts_at=time(10, 45),
        ends_at=time(12, 15),
        subject="Общая лекция",
        subgroup="3-СУЗСс-3",
        source_id="full-group",
    )
    subgroup_lesson = Lesson(
        day=date(2026, 9, 11),
        starts_at=time(10, 45),
        ends_at=time(12, 15),
        subject="Лабораторная",
        subgroup="1 подгруппа",
        source_id="subgroup",
    )
    envelope = _envelope(date(2026, 9, 7), full_group_lesson, subgroup_lesson)

    reminder = due_reminder(
        envelope,
        local_now=datetime(2026, 9, 11, 8, 45, tzinfo=UTC),
        sent_markers=(),
    )

    assert reminder is not None
    _, text = reminder
    assert "3-СУЗСс-3" not in text
    assert "1 подгруппа" in text


def test_current_schedule_renderers_never_add_a_source_link() -> None:
    old = _lesson(date(2026, 9, 11), room="312")
    new = _lesson(date(2026, 9, 11), room="407")
    previous = _envelope(date(2026, 9, 7), old)
    current = _envelope(date(2026, 9, 7), new)
    changes = overlapping_changes(previous, current, today=date(2026, 9, 11))
    reminder = due_reminder(
        current,
        local_now=datetime(2026, 9, 11, 8, 45, tzinfo=UTC),
        sent_markers=(),
    )

    assert reminder is not None
    rendered_messages = (
        render_rich_digest(
            current,
            local_now=datetime(2026, 9, 11, 8, 45, tzinfo=UTC),
        ),
        render_digest_fallback(
            current,
            local_now=datetime(2026, 9, 11, 8, 45, tzinfo=UTC),
        ),
        render_rich_changes(changes, fetched_at=current.fetched_at),
        render_changes_fallback(changes),
        reminder[1],
    )
    for rendered in rendered_messages:
        assert "Источник" not in rendered
        assert "rasp.spbgasu.ru" not in rendered

def test_delivery_state_survives_ci_cache_and_digest_is_once_per_day(tmp_path) -> None:
    path = tmp_path / "state" / "schedule.json"
    envelope = _envelope(date(2026, 8, 31), force_digest=False)
    state = ScheduleDeliveryState(
        previous=envelope,
        last_digest_date=date(2026, 9, 1),
        sent_reminders=("first:2026-09-01:10:45:00", "next:2026-09-01:12:15:00"),
        calendar_message_id=14979,
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
