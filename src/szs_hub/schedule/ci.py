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
from szs_hub.schedule.render import render_day_card

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
    calendar_feed_url: str | None = None,
) -> str:
    """Render one mobile-first calendar with independently expandable days."""

    fetched_local = envelope.fetched_at.astimezone(local_now.tzinfo)
    current_monday = local_now.date() - timedelta(days=local_now.date().weekday())
    start = max(envelope.horizon_start, current_monday)
    end = envelope.horizon_end
    blocks = [
        "<h1>🗓 Расписание</h1>",
        f"<p><b>{escape(envelope.group_key)}</b> · <i>{_date_range(start, end)}</i></p>",
        f"<aside>{_day_status(envelope, local_now)}</aside>",
        "<hr/>",
    ]
    for week_number, week_start in enumerate((start, start + timedelta(days=7))):
        if week_start > end:
            break
        week_end = min(week_start + timedelta(days=6), end)
        week_lessons = tuple(
            lesson for lesson in envelope.lessons if week_start <= lesson.day <= week_end
        )
        week_open = " open" if week_number == 0 else ""
        week_summary = _week_summary_line(week_number, week_start, week_end, week_lessons)
        blocks.append(
            f"<details{week_open}><summary>{week_summary}</summary>"
        )
        free_days: list[date] = []
        for day_offset in range((week_end - week_start).days + 1):
            day = week_start + timedelta(days=day_offset)
            lessons = _lessons_on(envelope.lessons, day)
            if not lessons:
                free_days.append(day)
                continue
            open_today = (
                " open"
                if day == local_now.date() and _has_upcoming(lessons, local_now)
                else ""
            )
            lesson_table = _rich_lesson_table(
                lessons,
                group_key=envelope.group_key,
                local_now=local_now,
            )
            blocks.append(
                f"<details{open_today}><summary>{_day_summary_line(day, lessons, local_now.date())}"
                f"</summary>{lesson_table}</details>"
            )
        if free_days:
            blocks.append(f"<p>☕ <i>Без пар: {_free_days_line(free_days)}</i></p>")
        blocks.append("</details>")
    if calendar_feed_url:
        blocks.append(
            "<hr/>"
            '<tg-button-row align="center">'
            f'<tg-button type="url" style="primary" url="{escape(calendar_feed_url)}">'
            "📲 Подключить календарь</tg-button></tg-button-row>"
        )
    blocks.append(f"<footer>Обновлено {fetched_local:%d.%m · %H:%M} МСК</footer>")
    return "".join(blocks)


def render_digest_fallback(
    envelope: ScheduleEnvelope,
    *,
    local_now: datetime,
    calendar_feed_url: str | None = None,
) -> str:
    primary_day, relative_label = _digest_target(local_now)
    lessons = _lessons_on(envelope.lessons, primary_day)
    text = render_day_card(primary_day, lessons, relative_label=relative_label)
    if calendar_feed_url:
        text += (
            f'\n\n<a href="{escape(calendar_feed_url)}">'
            "Добавить в календарь телефона</a>"
        )
    return text


def render_evening_summary(
    envelope: ScheduleEnvelope,
    *,
    local_now: datetime,
    calendar_url: str | None = None,
) -> str:
    day = local_now.date() + timedelta(days=1)
    lessons = _lessons_on(envelope.lessons, day)
    heading = f"<b>🌙 Завтра · {day.day} {_MONTHS[day.month]}</b>"
    if not lessons:
        body = "Занятий нет."
    else:
        slots = _slot_groups(lessons)
        lines = [heading, f"📚 {len(slots)} {_pair_word(len(slots))} · {_time_span(lessons)}"]
        for (starts_at, _ends_at), slot_lessons in slots:
            subjects = " / ".join(dict.fromkeys(escape(item.subject) for item in slot_lessons))
            place = _compact_slot_location(slot_lessons)
            lines.append(
                f"<b>• {starts_at:%H:%M}</b> · {subjects}{f' · 📍 {place}' if place else ''}"
            )
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
    grouped = _group_changes(changes)
    shown = grouped[:8]
    blocks = [
        "<h2>⚡ Расписание изменилось</h2>",
        "<aside><b>Календарь уже обновлён</b><br>"
        "Откройте нужную пару: внутри — изменение и событие целиком.</aside>",
        "<hr/>",
    ]
    for index, group in enumerate(shown):
        blocks.append(_rich_change_card(group, is_open=index == 0))
    grouped_count = len(grouped)
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
    groups = _group_changes(changes)[:8]
    parts = ["<b>Расписание изменилось</b>"]
    for group in groups:
        lesson = group[0].after or group[0].before
        assert lesson is not None
        parts.append(
            f"<b>{_change_status(group)} · {_event_date(lesson)}</b>\n"
            f"{_fallback_change_summary(group)}\n"
            f"{_fallback_event(lesson)}"
        )
    if len(_group_changes(changes)) > len(groups):
        parts.append(f"Ещё изменений: {len(_group_changes(changes)) - len(groups)}.")
    return "\n\n".join(parts)


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
                f"<b>⏰ Следующая в {next_start:%H:%M}</b>\n"
                f"<i>Через {_minutes_phrase(until_next)}</i>\n"
                f"{_reminder_block(next_start, tuple(next_lessons), group_key=envelope.group_key)}"
            )
            return marker, text

    first_start = ordered[0][0][0]
    first_at = _local_lesson_time(local_now, first_start)
    minutes_until = int((first_at - local_now).total_seconds() // 60)
    marker = f"first:{local_now.date().isoformat()}:{first_start}"
    if 90 <= minutes_until <= 130 and marker not in sent:
        text = (
            f"<b>🌅 Первая пара в {first_start:%H:%M}</b>\n"
            f"<i>Через {_minutes_phrase(minutes_until)}</i>\n"
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
        rows.append(f"<b>{subject}</b>{suffix}")
    return "\n\n".join(rows)


def _date_range(start: date, end: date) -> str:
    if start.month == end.month:
        return f"{start.day}–{end.day} {_MONTHS[start.month]}"
    return f"{start.day} {_MONTHS[start.month]} – {end.day} {_MONTHS[end.month]}"


def _day_name(day: date, today: date) -> str:
    if day == today:
        return f"<mark>Сегодня</mark> · {_SHORT_WEEKDAYS[day.weekday()]}, {day.day}"
    return f"{_SHORT_WEEKDAYS[day.weekday()]}, {day.day}"


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


def _week_summary_line(
    week_number: int,
    start: date,
    end: date,
    lessons: tuple[Lesson, ...],
) -> str:
    label = "🗓 Эта неделя" if week_number == 0 else "🔭 Следующая неделя"
    study_days = len({lesson.day for lesson in lessons})
    pair_count = len(_slot_groups_by_day(lessons))
    if not lessons:
        return f"<b>{label}</b> · {_date_range(start, end)} · без пар"
    return (
        f"<b>{label}</b> · {_date_range(start, end)} · "
        f"{study_days} {_day_word(study_days)} · {pair_count} {_pair_word(pair_count)}"
    )


def _slot_groups_by_day(lessons: tuple[Lesson, ...]) -> set[tuple[date, time, time]]:
    return {(lesson.day, lesson.starts_at, lesson.ends_at) for lesson in lessons}


def _day_word(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return "день"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return "дня"
    return "дней"


def _free_days_line(days: list[date]) -> str:
    return ", ".join(f"{_SHORT_WEEKDAYS[day.weekday()]} {day.day}" for day in days)


def _has_upcoming(lessons: tuple[Lesson, ...], local_now: datetime) -> bool:
    if not lessons or lessons[0].day != local_now.date():
        return False
    return any(item.ends_at > local_now.time().replace(tzinfo=None) for item in lessons)


def _day_status(envelope: ScheduleEnvelope, local_now: datetime) -> str:
    lessons = _lessons_on(envelope.lessons, local_now.date())
    if not lessons:
        return "☕ <b>Сегодня без пар</b><br><i>Можно выдохнуть</i>"
    now_time = local_now.time().replace(tzinfo=None)
    current = next(
        (item for item in lessons if item.starts_at <= now_time < item.ends_at),
        None,
    )
    if current is not None:
        return (
            f"🟢 <mark><b>Сейчас</b></mark> · {escape(current.subject)}"
            f"<br><i>До {current.ends_at:%H:%M}</i>"
        )
    upcoming = next((item for item in lessons if item.starts_at > now_time), None)
    if upcoming is not None:
        return (
            f"🔵 <b>Следующая · {upcoming.starts_at:%H:%M}</b>"
            f"<br>{escape(upcoming.subject)}"
        )
    return "🌙 <b>На сегодня всё</b><br><i>До завтра</i>"


def _compact_slot_location(lessons: tuple[Lesson, ...]) -> str:
    values = []
    for lesson in lessons:
        location = _location(lesson).replace("<br>", " · ")
        if location and location not in values:
            values.append(location)
    return " / ".join(values)


def _rich_lesson_table(
    lessons: tuple[Lesson, ...],
    *,
    group_key: str,
    local_now: datetime,
) -> str:
    rows: list[str] = []
    current_time = local_now.time().replace(tzinfo=None)
    today = local_now.date()
    upcoming_start = next(
        (
            lesson.starts_at
            for lesson in lessons
            if lesson.day == today and lesson.starts_at > current_time
        ),
        None,
    )
    for lesson in lessons:
        teacher = (
            escape(lesson.teacher.strip())
            if lesson.teacher and lesson.teacher.strip()
            else "Преподаватель не указан"
        )
        lesson_type = (
            escape(lesson.lesson_type.strip())
            if lesson.lesson_type and lesson.lesson_type.strip()
            else "—"
        )
        room = (
            escape(lesson.room.strip())
            if lesson.room and lesson.room.strip()
            else "—"
        )
        building = (
            escape(lesson.building.strip())
            if lesson.building and lesson.building.strip()
            else "—"
        )
        subgroup = (
            escape(lesson.subgroup.strip())
            if lesson.subgroup
            and lesson.subgroup.strip()
            and lesson.subgroup.strip().casefold() != group_key.strip().casefold()
            else None
        )
        is_current = (
            lesson.day == today and lesson.starts_at <= current_time < lesson.ends_at
        )
        is_next = lesson.day == today and lesson.starts_at == upcoming_start
        subject = f"<b>{escape(lesson.subject.strip())}</b>"
        if is_current:
            state_label = "<b>Сейчас</b><br>"
        elif is_next:
            state_label = "<mark><b>Далее</b></mark><br>"
        else:
            state_label = ""
        time_cell_tag = "th" if is_next else "td"
        row_span = 4 if subgroup else 3
        rows.append(
            f'<tr><{time_cell_tag} rowspan="{row_span}" '
            'align="center" valign="middle">'
            f"{state_label}<b>{lesson.starts_at:%H:%M}</b>"
            f"<br><i>{lesson.ends_at:%H:%M}</i></{time_cell_tag}>"
            f'<th colspan="3" align="center" valign="middle">{subject}</th></tr>'
            '<tr>'
            f'<td align="center" valign="middle"><i>Тип</i><br>{lesson_type}</td>'
            f'<td align="center" valign="middle"><i>Ауд.</i><br><b>{room}</b></td>'
            f'<td align="center" valign="middle"><i>Корп.</i><br><b>{building}</b></td>'
            '</tr>'
            f'<tr><td colspan="3" align="center" valign="middle">'
            f"👤 <i>{teacher}</i></td></tr>"
        )
        if subgroup:
            rows.append(
                f'<tr><td colspan="3" align="left" valign="middle">'
                f"👥 <i>{subgroup}</i></td></tr>"
            )
    return f"<table bordered compact>{''.join(rows)}</table>"


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


def _rich_change_card(changes: tuple[ScheduleChange, ...], *, is_open: bool) -> str:
    lesson = changes[0].after or changes[0].before
    assert lesson is not None
    status = _change_status(changes)
    open_attribute = " open" if is_open else ""
    summary = (
        f"<b>{status}</b> · {_SHORT_WEEKDAYS[lesson.day.weekday()]}, {lesson.day.day} · "
        f"{lesson.starts_at:%H:%M} · {escape(lesson.subject)}"
    )
    blocks = [
        f"<details{open_attribute}><summary>{summary}</summary>",
        _rich_change_summary(changes),
    ]
    changed_kinds = {change.kind for change in changes}
    if any(change.kind is ChangeKind.CANCELLED for change in changes):
        blocks.append(
            _rich_event_table(lesson, caption="🚨 Отменённая пара", highlighted=changed_kinds)
        )
    else:
        current = changes[0].after or lesson
        blocks.append(
            _rich_event_table(current, caption="✨ Актуальная пара", highlighted=changed_kinds)
        )
        previous = changes[0].before
        if previous is not None and all(change.kind is not ChangeKind.ADDED for change in changes):
            blocks.append(
                "<details><summary>↩️ Прежний вариант</summary>"
                f"{_rich_event_table(previous, caption=None, highlighted=set())}</details>"
            )
    blocks.append("</details>")
    return "".join(blocks)


def _change_status(changes: tuple[ScheduleChange, ...]) -> str:
    kinds = {change.kind for change in changes}
    if ChangeKind.ADDED in kinds:
        return "🆕 Добавлена"
    if ChangeKind.CANCELLED in kinds:
        return "🚨 Отменена"
    return "🔄 Обновлена"


def _rich_change_summary(changes: tuple[ScheduleChange, ...]) -> str:
    if any(change.kind is ChangeKind.ADDED for change in changes):
        return "<blockquote><mark><b>Новая пара</b></mark> добавлена в расписание</blockquote>"
    if any(change.kind is ChangeKind.CANCELLED for change in changes):
        return (
            "<blockquote><mark><b>Пара отменена</b></mark><br>"
            "Проверьте дату и время ниже.</blockquote>"
        )
    rows: list[str] = []
    for change in changes:
        assert change.before is not None and change.after is not None
        before, after = _change_values(change)
        rows.append(
            f"<b>{_change_label(change.kind)}</b><br>"
            f"<s>{escape(before)}</s> → <mark>{escape(after)}</mark>"
        )
    return f"<blockquote>{'<br>'.join(rows)}</blockquote>"


def _rich_event_table(
    lesson: Lesson,
    *,
    caption: str | None,
    highlighted: set[ChangeKind],
) -> str:
    values: list[tuple[str, str, ChangeKind | None]] = [
        ("Дата", _event_date(lesson), None),
        ("Время", f"{lesson.starts_at:%H:%M}–{lesson.ends_at:%H:%M}", ChangeKind.TIME),
        ("Предмет", lesson.subject, ChangeKind.SUBJECT),
        ("Тип", lesson.lesson_type or "не указан", ChangeKind.LESSON_TYPE),
        ("Где", _plain_location(lesson), ChangeKind.ROOM),
        ("Преподаватель", lesson.teacher or "не указан", ChangeKind.TEACHER),
        ("Подгруппа", lesson.subgroup or "вся группа", None),
    ]
    rows: list[str] = []
    for label, value, kind in values:
        rendered = escape(value)
        if kind in highlighted or (kind is ChangeKind.ROOM and ChangeKind.BUILDING in highlighted):
            rendered = f"<mark>{rendered}</mark>"
        rows.append(
            f'<tr><th align="left" valign="top">{label}</th>'
            f'<td align="left" valign="top">{rendered}</td></tr>'
        )
    caption_html = f"<caption><b>{caption}</b></caption>" if caption else ""
    return f"<table bordered compact>{caption_html}{''.join(rows)}</table>"


def _event_date(lesson: Lesson) -> str:
    return f"{_SHORT_WEEKDAYS[lesson.day.weekday()]}, {lesson.day.day} {_MONTHS[lesson.day.month]}"


def _plain_location(lesson: Lesson) -> str:
    room = lesson.room.strip() if lesson.room else ""
    building = lesson.building.strip() if lesson.building else ""
    if room and building:
        return f"ауд. {room}, корп. {building}"
    if room:
        return f"ауд. {room}"
    if building:
        return f"корп. {building}"
    return "не указано"


def _change_label(kind: ChangeKind) -> str:
    return {
        ChangeKind.TIME: "Время",
        ChangeKind.ROOM: "Аудитория",
        ChangeKind.BUILDING: "Корпус",
        ChangeKind.TEACHER: "Преподаватель",
        ChangeKind.LESSON_TYPE: "Тип занятия",
        ChangeKind.SUBJECT: "Предмет",
        ChangeKind.ADDED: "Добавлено",
        ChangeKind.CANCELLED: "Отменено",
    }[kind]


def _change_values(change: ScheduleChange) -> tuple[str, str]:
    assert change.before is not None and change.after is not None
    if change.kind is ChangeKind.TIME:
        return (
            f"{change.before.starts_at:%H:%M}–{change.before.ends_at:%H:%M}",
            f"{change.after.starts_at:%H:%M}–{change.after.ends_at:%H:%M}",
        )
    return _changed_value(change.before, change.kind), _changed_value(change.after, change.kind)


def _fallback_change_summary(changes: tuple[ScheduleChange, ...]) -> str:
    if any(change.kind is ChangeKind.ADDED for change in changes):
        return "Добавлена новая пара."
    if any(change.kind is ChangeKind.CANCELLED for change in changes):
        return "Пара отменена."
    rows = []
    for change in changes:
        before, after = _change_values(change)
        rows.append(
            f"{_change_label(change.kind)}: <s>{escape(before)}</s> → <b>{escape(after)}</b>"
        )
    return "\n".join(rows)


def _fallback_event(lesson: Lesson) -> str:
    return "\n".join(
        (
            f"<b>{escape(lesson.subject)}</b>",
            f"{_event_date(lesson)} · {lesson.starts_at:%H:%M}–{lesson.ends_at:%H:%M}",
            f"Тип: {escape(lesson.lesson_type or 'не указан')}",
            f"Где: {escape(_plain_location(lesson))}",
            f"Преподаватель: {escape(lesson.teacher or 'не указан')}",
            f"Подгруппа: {escape(lesson.subgroup or 'вся группа')}",
        )
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


def _changed_value(lesson: Lesson, kind: ChangeKind) -> str:
    values = {
        ChangeKind.ROOM: lesson.room,
        ChangeKind.BUILDING: lesson.building,
        ChangeKind.TEACHER: lesson.teacher,
        ChangeKind.LESSON_TYPE: lesson.lesson_type,
        ChangeKind.SUBJECT: lesson.subject,
    }
    return values[kind] or "не указано"
