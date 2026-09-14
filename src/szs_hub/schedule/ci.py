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

_SCHEMA_VERSION = 3
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
    calendar_message_id: int | None = None


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
            calendar_message_id=_optional_positive_int(payload.get("calendar_message_id")),
        )
    except (OSError, ValueError, TypeError, AttributeError, json.JSONDecodeError):
        return ScheduleDeliveryState()


def save_delivery_state(path: Path, state: ScheduleDeliveryState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": 3,
        "last_digest_date": (
            state.last_digest_date.isoformat() if state.last_digest_date else None
        ),
        "sent_reminders": list(state.sent_reminders[-100:]),
        "calendar_message_id": state.calendar_message_id,
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
    """Render one mobile-first calendar with independently expandable days."""

    fetched_local = envelope.fetched_at.astimezone(local_now.tzinfo)
    current_monday = local_now.date() - timedelta(days=local_now.date().weekday())
    start = max(envelope.horizon_start, current_monday)
    end = envelope.horizon_end
    blocks = [
        f"<h1>Расписание · {_date_range(start, min(start + timedelta(days=6), end))}</h1>",
        f"<p>{_day_status(envelope, local_now)}</p>",
    ]
    for week_start in (start, start + timedelta(days=7)):
        if week_start > end:
            break
        week_end = min(week_start + timedelta(days=6), end)
        if week_start != start:
            blocks.extend(("<hr/>", f"<h2>{_date_range(week_start, week_end)}</h2>"))
        for day_offset in range((week_end - week_start).days + 1):
            day = week_start + timedelta(days=day_offset)
            lessons = _lessons_on(envelope.lessons, day)
            if not lessons:
                blocks.append(f"<p><b>{_day_name(day, local_now.date())}</b> · занятий нет</p>")
                continue
            open_today = (
                " open"
                if day == local_now.date() and _has_upcoming(lessons, local_now)
                else ""
            )
            blocks.append(
                f"<details{open_today}><summary>{_day_summary_line(day, lessons, local_now.date())}"
                f"</summary>{_rich_lesson_list(lessons, group_key=envelope.group_key)}</details>"
            )
    blocks.append(f"<footer>Обновлено {fetched_local:%d.%m · %H:%M} МСК</footer>")
    return "".join(blocks)


def render_digest_fallback(
    envelope: ScheduleEnvelope,
    *,
    local_now: datetime,
) -> str:
    primary_day, relative_label = _digest_target(local_now)
    lessons = _lessons_on(envelope.lessons, primary_day)
    return render_day_card(primary_day, lessons, relative_label=relative_label)


def render_evening_summary(
    envelope: ScheduleEnvelope,
    *,
    local_now: datetime,
    calendar_url: str | None = None,
) -> str:
    day = local_now.date() + timedelta(days=1)
    lessons = _lessons_on(envelope.lessons, day)
    heading = f"<b>Завтра · {day.day} {_MONTHS[day.month]}</b>"
    if not lessons:
        body = "Занятий нет."
    else:
        slots = _slot_groups(lessons)
        lines = [heading, f"{len(slots)} {_pair_word(len(slots))} · {_time_span(lessons)}"]
        for (starts_at, _ends_at), slot_lessons in slots:
            subjects = " / ".join(dict.fromkeys(escape(item.subject) for item in slot_lessons))
            place = _compact_slot_location(slot_lessons)
            lines.append(f"<b>{starts_at:%H:%M}</b> · {subjects}{f' · {place}' if place else ''}")
        body = "\n".join(lines)
        heading = ""
    text = f"{heading}\n{body}".strip()
    if calendar_url:
        text += f'\n\n<a href="{escape(calendar_url)}">Открыть календарь</a>'
    return text


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
    shown = _group_changes(changes)[:8]
    blocks = ["<h2>⚠️ Расписание изменилось</h2>"]
    current_day: date | None = None
    for group in shown:
        lesson = group[0].after or group[0].before
        assert lesson is not None
        if lesson.day != current_day:
            current_day = lesson.day
            blocks.append(
                f"<h3>{_SHORT_WEEKDAYS[lesson.day.weekday()]}, "
                f"{lesson.day.day} {_MONTHS[lesson.day.month]}</h3>"
            )
        blocks.append(f"<p>{_rich_change_group(group)}</p>")
    grouped_count = len(_group_changes(changes))
    if grouped_count > len(shown):
        blocks.append(f"<p>И ещё {grouped_count - len(shown)} изменений.</p>")
    blocks.extend(
        (
            (
                f"<footer>Обновлено {fetched_at:%H:%M} МСК</footer>"
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
        if starts <= local_now < ends and 0 <= minutes_left <= 25:
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
                f"<b>Следующая в {next_start:%H:%M} · через {_minutes_phrase(until_next)}</b>\n"
                f"{_reminder_block(next_start, tuple(next_lessons), group_key=envelope.group_key)}"
            )
            return marker, text

    first_start = ordered[0][0][0]
    first_at = _local_lesson_time(local_now, first_start)
    minutes_until = int((first_at - local_now).total_seconds() // 60)
    marker = f"first:{local_now.date().isoformat()}:{first_start}"
    if 90 <= minutes_until <= 130 and marker not in sent:
        text = (
            f"<b>Первая в {first_start:%H:%M} · через {_minutes_phrase(minutes_until)}</b>\n"
            f"{_reminder_block(first_start, tuple(ordered[0][1]), group_key=envelope.group_key)}"
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
        "teacher": lesson.teacher,
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
        teacher=_optional_string(value, "teacher", 256),
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


def _optional_positive_int(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError("invalid positive integer")
    return value


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


def _reminder_block(
    starts_at: time,
    lessons: tuple[Lesson, ...],
    *,
    group_key: str,
) -> str:
    rows = []
    for lesson in lessons:
        subject = escape(lesson.subject.strip())
        details = []
        location = _location(lesson)
        if location:
            details.append(f"📍 {location.replace('<br>', ' · ')}")
        if lesson.subgroup and lesson.subgroup.strip().casefold() != group_key.strip().casefold():
            details.append(escape(lesson.subgroup.strip()))
        suffix = f"\n{' · '.join(details)}" if details else ""
        rows.append(f"{subject}{suffix}")
    return "\n\n".join(rows)


def _date_range(start: date, end: date) -> str:
    if start.month == end.month:
        return f"{start.day}–{end.day} {_MONTHS[start.month]}"
    return f"{start.day} {_MONTHS[start.month]} – {end.day} {_MONTHS[end.month]}"


def _day_name(day: date, today: date) -> str:
    suffix = " · Сегодня" if day == today else ""
    return f"{_SHORT_WEEKDAYS[day.weekday()]}, {day.day}{suffix}"


def _slot_groups(
    lessons: tuple[Lesson, ...],
) -> tuple[tuple[tuple[time, time], tuple[Lesson, ...]], ...]:
    grouped: dict[tuple[time, time], list[Lesson]] = {}
    for lesson in lessons:
        grouped.setdefault((lesson.starts_at, lesson.ends_at), []).append(lesson)
    return tuple(
        (
            slot,
            tuple(
                sorted(items, key=lambda item: (item.subject.casefold(), item.subgroup or ""))
            ),
        )
        for slot, items in sorted(grouped.items())
    )


def _time_span(lessons: tuple[Lesson, ...]) -> str:
    return f"{lessons[0].starts_at:%H:%M}–{max(item.ends_at for item in lessons):%H:%M}"


def _day_summary_line(day: date, lessons: tuple[Lesson, ...], today: date) -> str:
    count = len(_slot_groups(lessons))
    return f"{_day_name(day, today)} · {count} {_pair_word(count)} · {_time_span(lessons)}"


def _has_upcoming(lessons: tuple[Lesson, ...], local_now: datetime) -> bool:
    if not lessons or lessons[0].day != local_now.date():
        return False
    return any(item.ends_at > local_now.time().replace(tzinfo=None) for item in lessons)


def _day_status(envelope: ScheduleEnvelope, local_now: datetime) -> str:
    lessons = _lessons_on(envelope.lessons, local_now.date())
    if not lessons:
        return "Сегодня занятий нет"
    now_time = local_now.time().replace(tzinfo=None)
    current = next(
        (item for item in lessons if item.starts_at <= now_time < item.ends_at),
        None,
    )
    if current is not None:
        return f"Сейчас · <b>{escape(current.subject)}</b> · до {current.ends_at:%H:%M}"
    upcoming = next((item for item in lessons if item.starts_at > now_time), None)
    if upcoming is not None:
        return f"Следующая · <b>{upcoming.starts_at:%H:%M}</b> · {escape(upcoming.subject)}"
    return "На сегодня всё"


def _rich_lesson_list(lessons: tuple[Lesson, ...], *, group_key: str) -> str:
    blocks: list[str] = []
    for (starts_at, ends_at), slot_lessons in _slot_groups(lessons):
        for lesson in slot_lessons:
            meta: list[str] = []
            if lesson.lesson_type:
                meta.append(escape(lesson.lesson_type.strip()))
            location = _location(lesson).replace("<br>", " · ")
            if location:
                meta.append(f"ауд. {location}")
            if (
                lesson.subgroup
                and lesson.subgroup.strip().casefold() != group_key.strip().casefold()
            ):
                meta.append(escape(lesson.subgroup.strip()))
            lines = [
                f"<b>{starts_at:%H:%M}–{ends_at:%H:%M}</b>",
                escape(lesson.subject.strip()),
            ]
            if meta:
                lines.append(" · ".join(meta))
            lines.append(
                f"Преподаватель: {escape(lesson.teacher.strip())}"
                if lesson.teacher and lesson.teacher.strip()
                else "Преподаватель не указан"
            )
            blocks.append(f"<p>{'<br>'.join(lines)}</p>")
    return "".join(blocks)


def _compact_slot_location(lessons: tuple[Lesson, ...]) -> str:
    values = []
    for lesson in lessons:
        location = _location(lesson).replace("<br>", " · ")
        if location and location not in values:
            values.append(location)
    return " / ".join(values)


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
    count = len(_slot_groups(lessons))
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


def _change_identity(change: ScheduleChange) -> tuple[str, ...]:
    old = change.before
    new = change.after
    if old and new and old.source_id and old.source_id == new.source_id:
        return ("source", old.source_id)
    lesson = new or old
    assert lesson is not None
    return (
        "lesson",
        lesson.day.isoformat(),
        lesson.subject.casefold(),
        (lesson.subgroup or "").casefold(),
    )


def _group_changes(
    changes: tuple[ScheduleChange, ...],
) -> tuple[tuple[ScheduleChange, ...], ...]:
    grouped: dict[tuple[str, ...], list[ScheduleChange]] = {}
    for change in changes:
        grouped.setdefault(_change_identity(change), []).append(change)
    return tuple(tuple(items) for items in grouped.values())


def _rich_change_group(changes: tuple[ScheduleChange, ...]) -> str:
    if len(changes) == 1:
        return _rich_change(changes[0])
    lesson = changes[0].after or changes[0].before
    assert lesson is not None
    rows = [f"<b>{escape(lesson.subject)} · {lesson.starts_at:%H:%M}</b>"]
    labels = {
        ChangeKind.TIME: "Время",
        ChangeKind.ROOM: "Аудитория",
        ChangeKind.BUILDING: "Корпус",
        ChangeKind.TEACHER: "Преподаватель",
        ChangeKind.LESSON_TYPE: "Тип занятия",
        ChangeKind.SUBJECT: "Предмет",
    }
    for change in changes:
        if change.kind in (ChangeKind.ADDED, ChangeKind.CANCELLED):
            rows.append(_rich_change(change))
            continue
        assert change.before is not None and change.after is not None
        if change.kind is ChangeKind.TIME:
            before = f"{change.before.starts_at:%H:%M}–{change.before.ends_at:%H:%M}"
            after = f"{change.after.starts_at:%H:%M}–{change.after.ends_at:%H:%M}"
        else:
            before = _changed_value(change.before, change.kind)
            after = _changed_value(change.after, change.kind)
        rows.append(
            f"{labels[change.kind]}: <s>{escape(before)}</s> → <mark>{escape(after)}</mark>"
        )
    return "<br>".join(rows)


def _changed_value(lesson: Lesson, kind: ChangeKind) -> str:
    values = {
        ChangeKind.ROOM: lesson.room,
        ChangeKind.BUILDING: lesson.building,
        ChangeKind.TEACHER: lesson.teacher,
        ChangeKind.LESSON_TYPE: lesson.lesson_type,
        ChangeKind.SUBJECT: lesson.subject,
    }
    return values[kind] or "не указано"
