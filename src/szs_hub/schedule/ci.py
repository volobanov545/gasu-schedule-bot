"""Small durable schedule envelope and presentation for CI-only operation."""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from html import escape
from pathlib import Path
from typing import Any

from szs_hub.domain.schedule import (
    ChangeKind,
    Lesson,
    ScheduleChange,
    ScheduleSnapshot,
    diff_schedules,
)
from szs_hub.schedule.render import render_day_card, render_schedule_changes

_SCHEMA_VERSION = 2
_MAX_ENCODED_BYTES = 65_000
_MAX_LESSONS = 200
_WEEKDAYS = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)
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


@dataclass(frozen=True, slots=True)
class ScheduleEnvelope:
    """Validated two-week schedule passed from GitVerse to GitHub."""

    group_key: str
    fetched_at: datetime
    horizon_start: date
    horizon_end: date
    lessons: tuple[Lesson, ...]
    force_digest: bool = False

    def __post_init__(self) -> None:
        if not self.group_key.strip():
            raise ValueError("schedule envelope group cannot be empty")
        if self.fetched_at.tzinfo is None or self.fetched_at.utcoffset() is None:
            raise ValueError("schedule envelope timestamp must be timezone-aware")
        if self.horizon_start.weekday() != 0:
            raise ValueError("schedule envelope must start on Monday")
        if self.horizon_end != self.horizon_start + timedelta(days=13):
            raise ValueError("schedule envelope must cover exactly two weeks")
        if len(self.lessons) > _MAX_LESSONS:
            raise ValueError("schedule envelope has too many lessons")
        if any(
            lesson.day < self.horizon_start or lesson.day > self.horizon_end
            for lesson in self.lessons
        ):
            raise ValueError("schedule envelope lesson is outside its horizon")


@dataclass(frozen=True, slots=True)
class ScheduleDeliveryState:
    previous: ScheduleEnvelope | None = None
    last_digest_date: date | None = None
    sent_reminders: tuple[str, ...] = ()


def encode_schedule_envelope(envelope: ScheduleEnvelope) -> str:
    raw = json.dumps(
        _envelope_dict(envelope),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    encoded = base64.b64encode(raw).decode("ascii")
    if len(encoded) > _MAX_ENCODED_BYTES:
        raise ValueError("schedule envelope exceeds GitHub workflow input limit")
    return encoded


def decode_schedule_envelope(value: str) -> ScheduleEnvelope:
    if not value or len(value) > _MAX_ENCODED_BYTES:
        raise ValueError("invalid bridge schedule envelope")
    try:
        raw = base64.b64decode(value, validate=True)
        payload = json.loads(raw.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid bridge schedule envelope") from exc
    return _envelope_from_dict(payload)


def load_delivery_state(path: Path) -> ScheduleDeliveryState:
    """A missing or damaged cache safely becomes a quiet first-run baseline."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        previous_raw = payload.get("previous")
        previous = _envelope_from_dict(previous_raw) if previous_raw is not None else None
        digest_raw = payload.get("last_digest_date")
        digest_day = date.fromisoformat(digest_raw) if isinstance(digest_raw, str) else None
        reminders_raw = payload.get("sent_reminders", [])
        reminders = (
            tuple(item for item in reminders_raw if isinstance(item, str) and len(item) <= 100)
            if isinstance(reminders_raw, list)
            else ()
        )
        return ScheduleDeliveryState(
            previous=previous,
            last_digest_date=digest_day,
            sent_reminders=reminders[-100:],
        )
    except (OSError, ValueError, TypeError, AttributeError, json.JSONDecodeError):
        return ScheduleDeliveryState()


def save_delivery_state(path: Path, state: ScheduleDeliveryState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": 1,
        "last_digest_date": (
            state.last_digest_date.isoformat() if state.last_digest_date else None
        ),
        "sent_reminders": list(state.sent_reminders[-100:]),
        "previous": _envelope_dict(state.previous) if state.previous else None,
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )


def overlapping_changes(
    previous: ScheduleEnvelope | None,
    current: ScheduleEnvelope,
    *,
    today: date,
) -> tuple[ScheduleChange, ...]:
    """Ignore newly exposed future dates and expired past dates.

    This is the key distinction between a university publishing the next week and
    changing a date students had already seen.
    """

    if previous is None:
        return ()
    start = max(previous.horizon_start, current.horizon_start, today)
    end = min(previous.horizon_end, current.horizon_end)
    if start > end:
        return ()
    old = tuple(lesson for lesson in previous.lessons if start <= lesson.day <= end)
    new = tuple(lesson for lesson in current.lessons if start <= lesson.day <= end)
    return diff_schedules(
        ScheduleSnapshot("spbgasu", current.group_key, previous.fetched_at, old),
        ScheduleSnapshot("spbgasu", current.group_key, current.fetched_at, new),
    )


def should_publish_digest(
    state: ScheduleDeliveryState,
    envelope: ScheduleEnvelope,
    *,
    local_now: datetime,
) -> bool:
    if envelope.force_digest:
        return True
    return local_now.hour >= 20 and state.last_digest_date != local_now.date()


def render_rich_digest(
    envelope: ScheduleEnvelope,
    *,
    local_now: datetime,
) -> str:
    """Render Telegram Bot API 10.3 Rich HTML: useful first, details collapsed."""

    primary_day, relative_label = _digest_target(local_now)
    end = primary_day + timedelta(days=6)
    visible = tuple(lesson for lesson in envelope.lessons if primary_day <= lesson.day <= end)
    primary_lessons = _lessons_on(visible, primary_day)
    blocks = [
        f"<h1>📅 {relative_label} · {primary_day.day} {_MONTHS[primary_day.month]}</h1>",
        f"<p><b>{_WEEKDAYS[primary_day.weekday()].capitalize()}</b></p>",
    ]
    if primary_lessons:
        blocks.append(_rich_lesson_table(primary_lessons))
        blocks.append(f"<p>{_day_summary(primary_lessons)}</p>")
    else:
        blocks.append("<aside>Пар нет — можно выдохнуть.</aside>")

    week_rows = []
    for day_offset in range(7):
        day = primary_day + timedelta(days=day_offset)
        lessons = _lessons_on(visible, day)
        week_rows.append(
            f"<h3>{_SHORT_WEEKDAYS[day.weekday()]}, "
            f"{day.day} {_MONTHS[day.month]}</h3>"
        )
        week_rows.append(_rich_lesson_table(lessons) if lessons else "<p>Пар нет.</p>")
    blocks.extend(
        (
            "<hr/>",
            (
                f"<details><summary>Неделя · {primary_day.day} {_MONTHS[primary_day.month]} — "
                f"{end.day} {_MONTHS[end.month]}</summary>{''.join(week_rows)}</details>"
            ),
            (
                f"<footer>{escape(envelope.group_key)} · проверено "
                f"{envelope.fetched_at:%H:%M} МСК · СПбГАСУ</footer>"
            ),
        )
    )
    return "".join(blocks)


def render_digest_fallback(
    envelope: ScheduleEnvelope,
    *,
    local_now: datetime,
) -> str:
    primary_day, relative_label = _digest_target(local_now)
    lessons = _lessons_on(envelope.lessons, primary_day)
    day = render_day_card(primary_day, lessons, relative_label=relative_label)
    return day


def _digest_target(local_now: datetime) -> tuple[date, str]:
    """At night/morning show today; after the evening digest cutoff show tomorrow."""

    if local_now.hour < 20:
        return local_now.date(), "Сегодня"
    return local_now.date() + timedelta(days=1), "Завтра"


def render_rich_changes(
    changes: tuple[ScheduleChange, ...],
    *,
    fetched_at: datetime,
) -> str:
    shown = changes[:12]
    blocks = ["<h2>⚠️ Расписание изменилось</h2>"]
    current_day: date | None = None
    for change in shown:
        lesson = change.after or change.before
        assert lesson is not None
        if lesson.day != current_day:
            current_day = lesson.day
            blocks.append(
                f"<h3>{_SHORT_WEEKDAYS[lesson.day.weekday()]}, "
                f"{lesson.day.day} {_MONTHS[lesson.day.month]}</h3>"
            )
        blocks.append(f"<p>{_rich_change(change)}</p>")
    if len(changes) > len(shown):
        blocks.append(f"<p>И ещё {len(changes) - len(shown)} изменений.</p>")
    blocks.extend(
        (
            (
                f"<footer>Проверено {fetched_at:%H:%M} МСК · СПбГАСУ</footer>"
            ),
        )
    )
    return "".join(blocks)


def render_changes_fallback(changes: tuple[ScheduleChange, ...]) -> str:
    return render_schedule_changes(changes[:12], relative_label="в расписании")


def due_reminder(
    envelope: ScheduleEnvelope,
    *,
    local_now: datetime,
    sent_markers: tuple[str, ...],
) -> tuple[str, str] | None:
    """Return at most one concise class reminder for the current local time."""

    lessons = _lessons_on(envelope.lessons, local_now.date())
    if not lessons:
        return None
    sent = set(sent_markers)
    blocks: dict[tuple[time, time], list[Lesson]] = {}
    for lesson in lessons:
        blocks.setdefault((lesson.starts_at, lesson.ends_at), []).append(lesson)
    ordered = sorted(blocks.items())

    for (starts_at, ends_at), _current_lessons in ordered:
        starts = _local_lesson_time(local_now, starts_at)
        ends = _local_lesson_time(local_now, ends_at)
        minutes_left = int((ends - local_now).total_seconds() // 60)
        if starts <= local_now < ends and 5 <= minutes_left <= 25:
            next_blocks = [item for item in ordered if item[0][0] >= ends_at]
            if not next_blocks:
                return None
            (next_start, _), next_lessons = next_blocks[0]
            marker = f"next:{local_now.date().isoformat()}:{ends_at}:{next_start}"
            if marker in sent:
                return None
            until_next = int(
                (_local_lesson_time(local_now, next_start) - local_now).total_seconds()
                // 60
            )
            text = (
                f"⏳ <b>Следующая пара через {_minutes_phrase(until_next)}</b>\n"
                f"Текущая закончится через {_minutes_phrase(minutes_left)}.\n\n"
                f"{_reminder_block(next_start, tuple(next_lessons))}"
            )
            return marker, text

    first_start = ordered[0][0][0]
    first_at = _local_lesson_time(local_now, first_start)
    minutes_until = int((first_at - local_now).total_seconds() // 60)
    marker = f"first:{local_now.date().isoformat()}:{first_start}"
    if 105 <= minutes_until <= 130 and marker not in sent:
        text = (
            f"⏰ <b>Первая пара через {_minutes_phrase(minutes_until)}</b>\n\n"
            f"{_reminder_block(first_start, tuple(ordered[0][1]))}"
        )
        return marker, text
    return None


def changes_are_urgent(changes: tuple[ScheduleChange, ...], *, today: date) -> bool:
    tomorrow = today + timedelta(days=1)
    return any((change.after or change.before).day <= tomorrow for change in changes)  # type: ignore[union-attr]


def _envelope_dict(envelope: ScheduleEnvelope) -> dict[str, Any]:
    return {
        "v": _SCHEMA_VERSION,
        "group": envelope.group_key,
        "fetched_at": envelope.fetched_at.isoformat(),
        "horizon_start": envelope.horizon_start.isoformat(),
        "horizon_end": envelope.horizon_end.isoformat(),
        "force_digest": envelope.force_digest,
        "lessons": [_lesson_dict(lesson) for lesson in envelope.lessons],
    }


def _envelope_from_dict(value: object) -> ScheduleEnvelope:
    if not isinstance(value, dict) or value.get("v") != _SCHEMA_VERSION:
        raise ValueError("unsupported bridge schedule envelope")
    lessons_raw = value.get("lessons")
    if not isinstance(lessons_raw, list):
        raise ValueError("invalid bridge schedule lessons")
    fetched_at = datetime.fromisoformat(_required_string(value, "fetched_at", 64))
    return ScheduleEnvelope(
        group_key=_required_string(value, "group", 100),
        fetched_at=fetched_at,
        horizon_start=date.fromisoformat(_required_string(value, "horizon_start", 10)),
        horizon_end=date.fromisoformat(_required_string(value, "horizon_end", 10)),
        lessons=tuple(_lesson_from_dict(item) for item in lessons_raw),
        force_digest=value.get("force_digest") is True,
    )


def _lesson_dict(lesson: Lesson) -> dict[str, str | None]:
    return {
        "day": lesson.day.isoformat(),
        "starts_at": lesson.starts_at.isoformat(),
        "ends_at": lesson.ends_at.isoformat(),
        "subject": lesson.subject,
        "lesson_type": lesson.lesson_type,
        "room": lesson.room,
        "building": lesson.building,
        "subgroup": lesson.subgroup,
        "source_id": lesson.source_id,
    }


def _lesson_from_dict(value: object) -> Lesson:
    if not isinstance(value, dict):
        raise ValueError("invalid bridge schedule lesson")
    return Lesson(
        day=date.fromisoformat(_required_string(value, "day", 10)),
        starts_at=time.fromisoformat(_required_string(value, "starts_at", 16)),
        ends_at=time.fromisoformat(_required_string(value, "ends_at", 16)),
        subject=_required_string(value, "subject", 500),
        lesson_type=_optional_string(value, "lesson_type", 100),
        room=_optional_string(value, "room", 100),
        building=_optional_string(value, "building", 100),
        subgroup=_optional_string(value, "subgroup", 100),
        source_id=_optional_string(value, "source_id", 100),
    )


def _required_string(value: dict[str, Any], key: str, limit: int) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip() or len(item) > limit:
        raise ValueError(f"invalid bridge schedule field: {key}")
    return item.strip()


def _optional_string(value: dict[str, Any], key: str, limit: int) -> str | None:
    item = value.get(key)
    if item is None:
        return None
    if not isinstance(item, str) or len(item) > limit:
        raise ValueError(f"invalid bridge schedule field: {key}")
    return item.strip() or None


def _lessons_on(lessons: tuple[Lesson, ...], day: date) -> tuple[Lesson, ...]:
    return tuple(
        sorted(
            (lesson for lesson in lessons if lesson.day == day),
            key=lambda item: (item.starts_at, item.subject.casefold()),
        )
    )


def _local_lesson_time(local_now: datetime, value: time) -> datetime:
    return datetime.combine(local_now.date(), value, tzinfo=local_now.tzinfo)


def _minutes_phrase(minutes: int) -> str:
    if minutes == 120:
        return "2 часа"
    if minutes >= 60:
        hours, rest = divmod(minutes, 60)
        return f"{hours} ч {rest} мин" if rest else f"{hours} ч"
    return f"{minutes} мин"


def _reminder_block(starts_at: time, lessons: tuple[Lesson, ...]) -> str:
    rows = []
    for lesson in lessons:
        subject = escape(lesson.subject.strip())
        details = []
        location = _location(lesson)
        if location:
            details.append(f"📍 {location.replace('<br>', ' · ')}")
        if lesson.subgroup:
            details.append(escape(lesson.subgroup.strip()))
        suffix = f"\n{' · '.join(details)}" if details else ""
        rows.append(f"<b>{starts_at:%H:%M}</b> · {subject}{suffix}")
    return "\n\n".join(rows)


def _rich_lesson_table(lessons: tuple[Lesson, ...]) -> str:
    rows = ["<tr><th>Время</th><th>Предмет</th><th>Где</th></tr>"]
    for lesson in lessons:
        location = _location(lesson)
        subject = escape(lesson.subject.strip())
        if lesson.lesson_type:
            subject = f"{subject}<br><i>{escape(lesson.lesson_type.strip())}</i>"
        rows.append(
            f"<tr><td>{lesson.starts_at:%H:%M}–{lesson.ends_at:%H:%M}</td>"
            f"<td>{subject}</td><td>{location or '—'}</td></tr>"
        )
    return f"<table striped compact>{''.join(rows)}</table>"


def _location(lesson: Lesson) -> str:
    room = escape(lesson.room.strip()) if lesson.room else ""
    building = escape(lesson.building.strip()) if lesson.building else ""
    if room and building:
        return f"{room}<br><i>корп. {building}</i>"
    if room:
        return room
    if building:
        return f"корп. {building}"
    return ""


def _day_summary(lessons: tuple[Lesson, ...]) -> str:
    count = len(lessons)
    return (
        f"<b>{count} {_pair_word(count)}</b> · "
        f"{lessons[0].starts_at:%H:%M}–{lessons[-1].ends_at:%H:%M}"
    )


def _pair_word(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return "пара"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return "пары"
    return "пар"


def _rich_change(change: ScheduleChange) -> str:
    old = change.before
    new = change.after
    lesson = new or old
    assert lesson is not None
    title = f"<b>{escape(lesson.subject)} · {lesson.starts_at:%H:%M}</b>"
    if change.kind is ChangeKind.ADDED:
        assert new is not None
        return f"➕ {title}<br>Добавлена · {_location(new) or 'аудитория не указана'}"
    if change.kind is ChangeKind.CANCELLED:
        return f"❌ {title}<br>Пара отменена"
    assert old is not None and new is not None
    if change.kind is ChangeKind.TIME:
        before = f"{old.starts_at:%H:%M}–{old.ends_at:%H:%M}"
        after = f"{new.starts_at:%H:%M}–{new.ends_at:%H:%M}"
        return f"{title}<br>Время: <s>{before}</s> → <mark>{after}</mark>"
    labels = {
        ChangeKind.ROOM: "Аудитория",
        ChangeKind.BUILDING: "Корпус",
        ChangeKind.TEACHER: "Преподаватель",
        ChangeKind.LESSON_TYPE: "Тип занятия",
        ChangeKind.SUBJECT: "Предмет",
    }
    before_value = _changed_value(old, change.kind)
    after_value = _changed_value(new, change.kind)
    return (
        f"{title}<br>{labels[change.kind]}: "
        f"<s>{escape(before_value)}</s> → <mark>{escape(after_value)}</mark>"
    )


def _changed_value(lesson: Lesson, kind: ChangeKind) -> str:
    values = {
        ChangeKind.ROOM: lesson.room,
        ChangeKind.BUILDING: lesson.building,
        ChangeKind.TEACHER: lesson.teacher,
        ChangeKind.LESSON_TYPE: lesson.lesson_type,
        ChangeKind.SUBJECT: lesson.subject,
    }
    return values[kind] or "не указано"
