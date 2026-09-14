"""Standards-based read-only calendar feed for the public group schedule."""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256

from szs_hub.domain.schedule import Lesson
from szs_hub.schedule.ci import ScheduleEnvelope

_PRODUCT_ID = "-//Пятница//Расписание СПбГАСУ//RU"


def render_icalendar(envelope: ScheduleEnvelope) -> str:
    """Render one complete RFC 5545 feed; clients replace it on every refresh."""

    stamp = envelope.fetched_at.astimezone(UTC)
    sequence = int(stamp.timestamp())
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{_ical_escape(_PRODUCT_ID)}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_ical_escape(f'Пятница · {envelope.group_key}')}",
        "X-WR-TIMEZONE:Europe/Moscow",
        "REFRESH-INTERVAL;VALUE=DURATION:PT1H",
        "X-PUBLISHED-TTL:PT1H",
        "BEGIN:VTIMEZONE",
        "TZID:Europe/Moscow",
        "X-LIC-LOCATION:Europe/Moscow",
        "BEGIN:STANDARD",
        "TZOFFSETFROM:+0300",
        "TZOFFSETTO:+0300",
        "TZNAME:MSK",
        "DTSTART:19700101T000000",
        "END:STANDARD",
        "END:VTIMEZONE",
    ]
    for lesson in sorted(
        envelope.lessons,
        key=lambda item: (item.day, item.starts_at, item.subject.casefold(), item.source_id or ""),
    ):
        lines.extend(_event_lines(lesson, stamp=stamp, sequence=sequence))
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold_line(line) for line in lines) + "\r\n"


def _event_lines(lesson: Lesson, *, stamp: datetime, sequence: int) -> list[str]:
    uid_seed = lesson.source_id or "|".join(
        (
            lesson.day.isoformat(),
            lesson.subject.casefold(),
            (lesson.subgroup or "").casefold(),
        )
    )
    uid = f"{sha256(uid_seed.encode()).hexdigest()[:32]}@pyatnitsa-schedule"
    start = datetime.combine(lesson.day, lesson.starts_at)
    end = datetime.combine(lesson.day, lesson.ends_at)
    description = []
    if lesson.lesson_type:
        description.append(lesson.lesson_type.strip())
    if lesson.teacher:
        description.append(f"Преподаватель: {lesson.teacher.strip()}")
    if lesson.subgroup:
        description.append(f"Группа: {lesson.subgroup.strip()}")
    location = _location(lesson)
    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{stamp:%Y%m%dT%H%M%SZ}",
        f"LAST-MODIFIED:{stamp:%Y%m%dT%H%M%SZ}",
        f"SEQUENCE:{sequence}",
        f"DTSTART;TZID=Europe/Moscow:{start:%Y%m%dT%H%M%S}",
        f"DTEND;TZID=Europe/Moscow:{end:%Y%m%dT%H%M%S}",
        f"SUMMARY:{_ical_escape(lesson.subject.strip())}",
    ]
    if location:
        lines.append(f"LOCATION:{_ical_escape(location)}")
    if description:
        lines.append(f"DESCRIPTION:{_ical_escape(chr(10).join(description))}")
    lines.extend(("TRANSP:OPAQUE", "END:VEVENT"))
    return lines


def _location(lesson: Lesson) -> str:
    values = []
    if lesson.room:
        values.append(f"ауд. {lesson.room.strip()}")
    if lesson.building:
        values.append(f"корпус {lesson.building.strip()}")
    return " · ".join(values)


def _ical_escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace(";", "\\;")
        .replace(",", "\\,")
    )


def _fold_line(value: str, limit: int = 75) -> str:
    """Fold without splitting a UTF-8 code point; continuation begins with one space."""

    chunks: list[str] = []
    current = ""
    current_bytes = 0
    for character in value:
        width = len(character.encode("utf-8"))
        allowed = limit if not chunks else limit - 1
        if current and current_bytes + width > allowed:
            chunks.append(current)
            current = character
            current_bytes = width
        else:
            current += character
            current_bytes += width
    chunks.append(current)
    return "\r\n ".join(chunks)
