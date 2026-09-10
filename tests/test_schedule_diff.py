from __future__ import annotations

from datetime import UTC, date, datetime, time

import pytest

from szs_hub.domain.schedule import ChangeKind, Lesson, ScheduleSnapshot, diff_schedules


def lesson(**overrides: object) -> Lesson:
    values: dict[str, object] = {
        "day": date(2026, 9, 1),
        "starts_at": time(10, 0),
        "ends_at": time(11, 30),
        "subject": "Железобетонные конструкции",
        "lesson_type": "Практика",
        "teacher": "Иванов А. А.",
        "room": "312",
        "building": "1",
    }
    values.update(overrides)
    return Lesson(**values)  # type: ignore[arg-type]


def snapshot(*lessons: Lesson, group: str = "СЗС-3") -> ScheduleSnapshot:
    return ScheduleSnapshot("spbgasu", group, datetime(2026, 8, 29, tzinfo=UTC), lessons)


def test_room_change_is_one_semantic_change() -> None:
    changes = diff_schedules(snapshot(lesson()), snapshot(lesson(room="407")))

    assert [change.kind for change in changes] == [ChangeKind.ROOM]


def test_time_change_matches_the_same_lesson() -> None:
    changes = diff_schedules(
        snapshot(lesson()),
        snapshot(lesson(starts_at=time(11, 40), ends_at=time(13, 10))),
    )

    assert [change.kind for change in changes] == [ChangeKind.TIME]


def test_added_and_cancelled_are_not_hidden() -> None:
    added = lesson(subject="Геодезия", starts_at=time(11, 40), ends_at=time(13, 10))

    assert [change.kind for change in diff_schedules(snapshot(), snapshot(added))] == [
        ChangeKind.ADDED
    ]
    assert [change.kind for change in diff_schedules(snapshot(added), snapshot())] == [
        ChangeKind.CANCELLED
    ]


def test_source_id_survives_subject_rename() -> None:
    changes = diff_schedules(
        snapshot(lesson(subject="ЖБК", source_id="lesson-1")),
        snapshot(lesson(subject="Железобетон", source_id="lesson-1")),
    )

    assert [change.kind for change in changes] == [ChangeKind.SUBJECT]


def test_snapshot_hash_is_order_independent() -> None:
    first = lesson()
    second = lesson(subject="Геодезия", starts_at=time(11, 40), ends_at=time(13, 10))

    assert snapshot(first, second).content_hash == snapshot(second, first).content_hash


def test_diff_rejects_different_groups() -> None:
    with pytest.raises(ValueError, match="different groups"):
        diff_schedules(snapshot(group="A"), snapshot(group="B"))

