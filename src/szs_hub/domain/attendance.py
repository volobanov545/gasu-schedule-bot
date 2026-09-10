"""Idempotent attendance reaction policy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class AttendanceAction(StrEnum):
    MARK = "mark"
    UNMARK = "unmark"
    IGNORE_DUPLICATE = "ignore_duplicate"
    IGNORE_WRONG_REACTION = "ignore_wrong_reaction"
    IGNORE_OUTSIDE_WINDOW = "ignore_outside_window"


@dataclass(frozen=True, slots=True)
class AttendanceSession:
    lesson_key: str
    message_id: int
    opens_at: datetime
    closes_at: datetime
    reaction: str = "❤"

    def __post_init__(self) -> None:
        if self.opens_at.tzinfo is None or self.closes_at.tzinfo is None:
            raise ValueError("attendance timestamps must be timezone-aware")
        if self.closes_at <= self.opens_at:
            raise ValueError("attendance window must have positive duration")
        if not self.reaction:
            raise ValueError("attendance reaction cannot be empty")


@dataclass(frozen=True, slots=True)
class ReactionEvent:
    user_id: int
    occurred_at: datetime
    reaction: str
    is_present: bool

    def __post_init__(self) -> None:
        if self.occurred_at.tzinfo is None:
            raise ValueError("reaction event timestamp must be timezone-aware")


def decide_attendance_action(
    session: AttendanceSession,
    event: ReactionEvent,
    currently_marked: bool,
) -> AttendanceAction:
    """Decide a DB mutation without relying on delivery order or duplicate-free updates."""

    if event.reaction != session.reaction:
        return AttendanceAction.IGNORE_WRONG_REACTION
    if event.occurred_at < session.opens_at or event.occurred_at >= session.closes_at:
        return AttendanceAction.IGNORE_OUTSIDE_WINDOW
    if event.is_present == currently_marked:
        return AttendanceAction.IGNORE_DUPLICATE
    return AttendanceAction.MARK if event.is_present else AttendanceAction.UNMARK

