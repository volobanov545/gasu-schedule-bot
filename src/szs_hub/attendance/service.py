"""Transactional attendance sessions driven by one explicit Telegram reaction."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from html import escape

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from szs_hub.domain.attendance import (
    AttendanceAction,
    ReactionEvent,
    decide_attendance_action,
)
from szs_hub.domain.attendance import AttendanceSession as DomainSession
from szs_hub.storage.models import (
    AttendanceMark,
    AttendanceSession,
    Membership,
    User,
)

_ACTIVE_ROSTER_STATUSES = ("creator", "administrator", "member")


@dataclass(frozen=True, slots=True)
class AttendanceUpdate:
    """Result of applying a reaction update to a durable session."""

    action: AttendanceAction
    session_id: int | None
    present_count: int | None


@dataclass(frozen=True, slots=True)
class AttendanceSummary:
    """A stable roster projection for one headman summary message."""

    session_id: int
    present: tuple[str, ...]
    missing: tuple[str, ...]

    @property
    def roster_size(self) -> int:
        return len(self.present) + len(self.missing)


class AttendanceService:
    """Persist add/remove reactions while an attendance window is open.

    The service performs no Telegram calls. Callers can edit one existing headman
    summary after a successful MARK/UNMARK result, avoiding a message per reaction.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def open_session(
        self,
        *,
        lesson_id: int,
        chat_id: int,
        message_id: int,
        starts_at: datetime,
        window_minutes: int,
        reaction: str,
    ) -> AttendanceSession:
        """Create a session once; a retry returns the original compatible row."""

        if starts_at.tzinfo is None:
            raise ValueError("attendance start must be timezone-aware")
        if not 5 <= window_minutes <= 120:
            raise ValueError("attendance window must be between 5 and 120 minutes")
        cleaned_reaction = reaction.strip()
        if not cleaned_reaction:
            raise ValueError("attendance reaction cannot be empty")

        existing = await self._session.scalar(
            select(AttendanceSession).where(AttendanceSession.lesson_id == lesson_id)
        )
        if existing is not None:
            if (
                existing.chat_id != chat_id
                or existing.message_id != message_id
                or existing.reaction != cleaned_reaction
            ):
                raise ValueError("lesson already has a different attendance session")
            return existing

        created = AttendanceSession(
            lesson_id=lesson_id,
            chat_id=chat_id,
            message_id=message_id,
            reaction=cleaned_reaction,
            opens_at=starts_at,
            closes_at=starts_at + timedelta(minutes=window_minutes),
            status="open",
        )
        self._session.add(created)
        await self._session.flush()
        return created

    async def apply_reaction(
        self,
        *,
        chat_id: int,
        message_id: int,
        event: ReactionEvent,
        display_name: str | None,
        username: str | None = None,
        source_update_id: int | None = None,
    ) -> AttendanceUpdate:
        """Apply one normalized reaction event idempotently and fail closed."""

        session = await self._session.scalar(
            select(AttendanceSession).where(
                AttendanceSession.chat_id == chat_id,
                AttendanceSession.message_id == message_id,
            )
        )
        if session is None:
            return AttendanceUpdate(AttendanceAction.IGNORE_OUTSIDE_WINDOW, None, None)

        user = await self._get_or_create_user(
            telegram_user_id=event.user_id,
            display_name=display_name,
            username=username,
            seen_at=event.occurred_at,
        )
        mark = await self._session.scalar(
            select(AttendanceMark).where(
                AttendanceMark.session_id == session.id,
                AttendanceMark.user_id == user.id,
            )
        )
        currently_marked = mark is not None and mark.removed_at is None
        domain_session = DomainSession(
            lesson_key=str(session.lesson_id),
            message_id=session.message_id,
            opens_at=session.opens_at,
            closes_at=session.closes_at,
            reaction=session.reaction,
        )
        action = decide_attendance_action(domain_session, event, currently_marked)
        if session.status not in {"scheduled", "open"}:
            action = AttendanceAction.IGNORE_OUTSIDE_WINDOW

        if action is AttendanceAction.MARK:
            if mark is None:
                mark = AttendanceMark(
                    session_id=session.id,
                    user_id=user.id,
                    source_update_id=source_update_id,
                    marked_at=event.occurred_at,
                )
                self._session.add(mark)
            else:
                mark.marked_at = event.occurred_at
                mark.removed_at = None
                mark.source_update_id = source_update_id
        elif action is AttendanceAction.UNMARK and mark is not None:
            mark.removed_at = event.occurred_at

        if action in {AttendanceAction.MARK, AttendanceAction.UNMARK}:
            await self._session.flush()
        return AttendanceUpdate(action, session.id, await self._present_count(session.id))

    async def close_due(self, *, now: datetime) -> int:
        """Close expired windows; reactions delivered later remain ignored."""

        if now.tzinfo is None:
            raise ValueError("attendance close time must be timezone-aware")
        closed_ids = await self._session.scalars(
            update(AttendanceSession)
            .where(
                AttendanceSession.status.in_(("scheduled", "open")),
                AttendanceSession.closes_at <= now,
            )
            .values(status="closed")
            .returning(AttendanceSession.id)
        )
        return len(tuple(closed_ids))

    async def summary(self, *, session_id: int) -> AttendanceSummary:
        """Return confirmed active members split by their current mark."""

        session = await self._session.get(AttendanceSession, session_id)
        if session is None:
            raise LookupError("attendance session does not exist")

        roster_rows = (
            await self._session.execute(
                select(User.id, User.display_name, User.username, User.telegram_user_id)
                .join(Membership, Membership.user_id == User.id)
                .where(
                    Membership.chat_id == session.chat_id,
                    Membership.status.in_(_ACTIVE_ROSTER_STATUSES),
                    User.is_bot.is_(False),
                )
                .order_by(User.display_name, User.telegram_user_id)
            )
        ).all()
        present_ids = set(
            await self._session.scalars(
                select(AttendanceMark.user_id).where(
                    AttendanceMark.session_id == session_id,
                    AttendanceMark.removed_at.is_(None),
                )
            )
        )
        present: list[str] = []
        missing: list[str] = []
        for user_id, display_name, username, telegram_user_id in roster_rows:
            label = _user_label(display_name, username, int(telegram_user_id))
            (present if int(user_id) in present_ids else missing).append(label)
        return AttendanceSummary(session_id, tuple(present), tuple(missing))

    async def _present_count(self, session_id: int) -> int:
        ids = await self._session.scalars(
            select(AttendanceMark.id).where(
                AttendanceMark.session_id == session_id,
                AttendanceMark.removed_at.is_(None),
            )
        )
        return len(tuple(ids))

    async def _get_or_create_user(
        self,
        *,
        telegram_user_id: int,
        display_name: str | None,
        username: str | None,
        seen_at: datetime,
    ) -> User:
        user = await self._session.scalar(
            select(User).where(User.telegram_user_id == telegram_user_id)
        )
        if user is None:
            user = User(
                telegram_user_id=telegram_user_id,
                display_name=display_name,
                username=username,
                last_seen_at=seen_at,
            )
            self._session.add(user)
            await self._session.flush()
            return user
        user.display_name = display_name or user.display_name
        user.username = username or user.username
        user.last_seen_at = max(user.last_seen_at, seen_at)
        return user


def render_attendance_summary(
    *,
    lesson_title: str,
    starts_at: datetime,
    summary: AttendanceSummary,
) -> str:
    """Render a compact HTML-safe message intended to be edited in place."""

    present = "\n".join(f"• {escape(name)}" for name in summary.present) or "—"
    missing = "\n".join(f"• {escape(name)}" for name in summary.missing) or "—"
    return (
        f"<b>{escape(lesson_title.strip() or 'Пара')} · {starts_at:%H:%M}</b>\n\n"
        f"Отметились: <b>{len(summary.present)} / {summary.roster_size}</b>\n"
        f"{present}\n\n"
        f"Не отметились: <b>{len(summary.missing)}</b>\n"
        f"{missing}\n\n"
        "<i>Добровольная отметка, не подтверждение геолокации.</i>"
    )


def _user_label(display_name: str | None, username: str | None, telegram_user_id: int) -> str:
    if display_name and display_name.strip():
        return display_name.strip()
    if username and username.strip():
        return f"@{username.strip().lstrip('@')}"
    return f"Telegram {telegram_user_id}"
