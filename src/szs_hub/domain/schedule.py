"""Schedule models and semantic change detection."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time
from difflib import SequenceMatcher
from enum import StrEnum

_WHITESPACE = re.compile(r"\s+")


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    return _WHITESPACE.sub(" ", value).strip().casefold().replace("ё", "е")


@dataclass(frozen=True, slots=True)
class Lesson:
    day: date
    starts_at: time
    ends_at: time
    subject: str
    lesson_type: str | None = None
    teacher: str | None = None
    room: str | None = None
    building: str | None = None
    subgroup: str | None = None
    source_id: str | None = None

    def __post_init__(self) -> None:
        if self.ends_at <= self.starts_at:
            raise ValueError("lesson end must be after its start")
        if not self.subject.strip():
            raise ValueError("lesson subject cannot be empty")

    @property
    def normalized_subject(self) -> str:
        return normalize_text(self.subject)

    def canonical(self) -> dict[str, str | None]:
        return {
            "day": self.day.isoformat(),
            "starts_at": self.starts_at.isoformat(),
            "ends_at": self.ends_at.isoformat(),
            "subject": self.normalized_subject,
            "lesson_type": normalize_text(self.lesson_type),
            "teacher": normalize_text(self.teacher),
            "room": normalize_text(self.room),
            "building": normalize_text(self.building),
            "subgroup": normalize_text(self.subgroup),
            "source_id": self.source_id,
        }


@dataclass(frozen=True, slots=True)
class ScheduleSnapshot:
    source: str
    group_key: str
    fetched_at: datetime
    lessons: tuple[Lesson, ...]

    def __post_init__(self) -> None:
        if self.fetched_at.tzinfo is None:
            raise ValueError("snapshot fetched_at must be timezone-aware")

    @property
    def content_hash(self) -> str:
        payload = [lesson.canonical() for lesson in sorted(self.lessons, key=_lesson_sort_key)]
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()


class ChangeKind(StrEnum):
    ADDED = "added"
    CANCELLED = "cancelled"
    TIME = "time"
    ROOM = "room"
    BUILDING = "building"
    TEACHER = "teacher"
    LESSON_TYPE = "lesson_type"
    SUBJECT = "subject"


@dataclass(frozen=True, slots=True)
class ScheduleChange:
    kind: ChangeKind
    before: Lesson | None
    after: Lesson | None

    def __post_init__(self) -> None:
        if self.kind is ChangeKind.ADDED and (self.before is not None or self.after is None):
            raise ValueError("added change requires only an after lesson")
        if self.kind is ChangeKind.CANCELLED and (self.before is None or self.after is not None):
            raise ValueError("cancelled change requires only a before lesson")
        if (
            self.kind not in (ChangeKind.ADDED, ChangeKind.CANCELLED)
            and (self.before is None or self.after is None)
        ):
            raise ValueError("field change requires before and after lessons")

    @property
    def fingerprint(self) -> str:
        data = {
            "kind": self.kind,
            "before": self.before.canonical() if self.before else None,
            "after": self.after.canonical() if self.after else None,
        }
        encoded = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()


def diff_schedules(
    previous: ScheduleSnapshot,
    current: ScheduleSnapshot,
) -> tuple[ScheduleChange, ...]:
    """Return user-meaningful changes, matching lessons conservatively."""

    if previous.group_key != current.group_key:
        raise ValueError("cannot diff schedules for different groups")

    pairs, old_unmatched, new_unmatched = _match_lessons(previous.lessons, current.lessons)
    changes: list[ScheduleChange] = []

    for old, new in pairs:
        if old.starts_at != new.starts_at or old.ends_at != new.ends_at:
            changes.append(ScheduleChange(ChangeKind.TIME, old, new))
        if normalize_text(old.room) != normalize_text(new.room):
            changes.append(ScheduleChange(ChangeKind.ROOM, old, new))
        if normalize_text(old.building) != normalize_text(new.building):
            changes.append(ScheduleChange(ChangeKind.BUILDING, old, new))
        if normalize_text(old.teacher) != normalize_text(new.teacher):
            changes.append(ScheduleChange(ChangeKind.TEACHER, old, new))
        if normalize_text(old.lesson_type) != normalize_text(new.lesson_type):
            changes.append(ScheduleChange(ChangeKind.LESSON_TYPE, old, new))
        if old.normalized_subject != new.normalized_subject:
            changes.append(ScheduleChange(ChangeKind.SUBJECT, old, new))

    changes.extend(ScheduleChange(ChangeKind.CANCELLED, lesson, None) for lesson in old_unmatched)
    changes.extend(ScheduleChange(ChangeKind.ADDED, None, lesson) for lesson in new_unmatched)
    return tuple(sorted(changes, key=_change_sort_key))


def _match_lessons(
    old_lessons: tuple[Lesson, ...],
    new_lessons: tuple[Lesson, ...],
) -> tuple[list[tuple[Lesson, Lesson]], list[Lesson], list[Lesson]]:
    pairs: list[tuple[Lesson, Lesson]] = []
    remaining_old = set(range(len(old_lessons)))
    remaining_new = set(range(len(new_lessons)))

    old_by_source = {
        lesson.source_id: index
        for index, lesson in enumerate(old_lessons)
        if lesson.source_id
    }
    for new_index, lesson in enumerate(new_lessons):
        old_index = old_by_source.get(lesson.source_id) if lesson.source_id else None
        if old_index is not None and old_index in remaining_old:
            pairs.append((old_lessons[old_index], lesson))
            remaining_old.remove(old_index)
            remaining_new.remove(new_index)

    candidates: list[tuple[float, float, int, int]] = []
    for old_index in remaining_old:
        for new_index in remaining_new:
            score = _match_score(old_lessons[old_index], new_lessons[new_index])
            if score >= 70:
                old_minutes = _time_minutes(old_lessons[old_index].starts_at)
                new_minutes = _time_minutes(new_lessons[new_index].starts_at)
                candidates.append((score, -abs(old_minutes - new_minutes), old_index, new_index))

    for _score, _time_proximity, old_index, new_index in sorted(candidates, reverse=True):
        if old_index not in remaining_old or new_index not in remaining_new:
            continue
        pairs.append((old_lessons[old_index], new_lessons[new_index]))
        remaining_old.remove(old_index)
        remaining_new.remove(new_index)

    return (
        pairs,
        [old_lessons[index] for index in sorted(remaining_old)],
        [new_lessons[index] for index in sorted(remaining_new)],
    )


def _match_score(old: Lesson, new: Lesson) -> float:
    if old.day != new.day:
        return float("-inf")
    subject_ratio = SequenceMatcher(None, old.normalized_subject, new.normalized_subject).ratio()
    score = subject_ratio * 70
    if old.normalized_subject == new.normalized_subject:
        score += 25
    if old.starts_at == new.starts_at:
        score += 15
    if normalize_text(old.subgroup) == normalize_text(new.subgroup):
        score += 5
    if normalize_text(old.lesson_type) == normalize_text(new.lesson_type):
        score += 5
    return score


def _lesson_sort_key(lesson: Lesson) -> tuple[date, time, str, str]:
    return lesson.day, lesson.starts_at, lesson.normalized_subject, lesson.source_id or ""


def _change_sort_key(change: ScheduleChange) -> tuple[date, time, str, str]:
    lesson = change.after or change.before
    assert lesson is not None
    return lesson.day, lesson.starts_at, lesson.normalized_subject, change.kind


def _time_minutes(value: time) -> int:
    return value.hour * 60 + value.minute
