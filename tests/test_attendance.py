from __future__ import annotations

from datetime import UTC, datetime, timedelta

from szs_hub.domain.attendance import (
    AttendanceAction,
    AttendanceSession,
    ReactionEvent,
    decide_attendance_action,
)


def session() -> AttendanceSession:
    starts = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
    return AttendanceSession("lesson-1", 42, starts, starts + timedelta(minutes=25))


def event(*, present: bool, reaction: str = "❤", minutes: int = 5) -> ReactionEvent:
    return ReactionEvent(
        user_id=7,
        occurred_at=session().opens_at + timedelta(minutes=minutes),
        reaction=reaction,
        is_present=present,
    )


def test_heart_add_and_remove_mutate_open_session() -> None:
    assert decide_attendance_action(session(), event(present=True), False) is AttendanceAction.MARK
    assert (
        decide_attendance_action(session(), event(present=False), True)
        is AttendanceAction.UNMARK
    )


def test_duplicate_delivery_is_idempotent() -> None:
    assert (
        decide_attendance_action(session(), event(present=True), True)
        is AttendanceAction.IGNORE_DUPLICATE
    )


def test_other_reactions_never_mark_attendance() -> None:
    assert (
        decide_attendance_action(session(), event(present=True, reaction="😂"), False)
        is AttendanceAction.IGNORE_WRONG_REACTION
    )


def test_close_boundary_is_fail_closed() -> None:
    assert (
        decide_attendance_action(session(), event(present=True, minutes=25), False)
        is AttendanceAction.IGNORE_OUTSIDE_WINDOW
    )
