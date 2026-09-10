"""Post-send durable state transitions for internal SZS Hub envelopes."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from szs_hub.attendance.service import AttendanceService
from szs_hub.config import FeatureProfile, Settings
from szs_hub.jobs.queue import OutboxClaim
from szs_hub.storage.base import utc_now
from szs_hub.storage.models import (
    AttendanceSession,
    Material,
    ScheduleChange,
    SystemSetting,
)
from szs_hub.telegram.access import Membership, can_access_group_history
from szs_hub.telegram.sender import TelegramCopyResult, TelegramOutboxSender


class HeadmanMembershipGateway(Protocol):
    async def current_membership(self, *, chat_id: int, user_id: int) -> Membership | None: ...


class PrivateRecipientAccessDenied(PermissionError):
    """A private outbox recipient is not the currently authorized headman."""


class ScheduleOnlyEnvelopeDenied(PermissionError):
    """An outbox envelope is outside the schedule-only publishing surface."""


class ApplicationTelegramSender:
    """Decorate the strict Telegram sender with idempotent local confirmations."""

    def __init__(
        self,
        *,
        sender: TelegramOutboxSender,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        membership_gateway: HeadmanMembershipGateway | None = None,
    ) -> None:
        self._sender = sender
        self._sessions = session_factory
        self._settings = settings
        self._membership_gateway = membership_gateway

    async def send(self, message: OutboxClaim) -> int | None:
        if self._settings.feature_profile is FeatureProfile.SCHEDULE_ONLY:
            self._authorize_schedule_only(message)
        await self._authorize_private_recipient(message.chat_id)
        attendance = _attendance_metadata(message.payload)
        material_copy = _material_copy_metadata(message.payload)
        if attendance is not None:
            async with self._sessions() as session:
                existing = await session.scalar(
                    select(AttendanceSession).where(
                        AttendanceSession.lesson_id == attendance.lesson_id
                    )
                )
                if existing is not None:
                    return existing.message_id

        if material_copy is not None:
            async with self._sessions() as session:
                material = await session.get(Material, material_copy.material_id)
                if material is None:
                    raise ValueError("material copy references a missing material")
                copied_source_ids = _evidence_ints(
                    material.evidence,
                    "copied_source_message_ids",
                )
                if set(material_copy.source_message_ids).issubset(copied_source_ids):
                    if material.destination_message_id is None:
                        raise ValueError("completed material has no destination message")
                    return material.destination_message_id
                if material.workflow_state not in {"copy_pending", "completed"}:
                    raise ValueError("material is not ready to be copied")

        send_result = await self._sender.send(message)
        if send_result is None:
            if material_copy is not None:
                raise ValueError("material copy returned no Telegram message ID")
            return None
        destination_ids = (
            send_result.message_ids
            if isinstance(send_result, TelegramCopyResult)
            else (send_result,)
        )
        telegram_message_id = destination_ids[0]
        if material_copy is not None and len(destination_ids) != material_copy.expected_count:
            raise ValueError("material copy result count does not match its durable envelope")
        async with self._sessions() as session, session.begin():
            if attendance is not None:
                await AttendanceService(session).open_session(
                    lesson_id=attendance.lesson_id,
                    chat_id=_required_chat_id(message),
                    message_id=telegram_message_id,
                    starts_at=attendance.starts_at,
                    window_minutes=self._settings.attendance_window_minutes,
                    reaction=self._settings.attendance_reaction,
                )
            state_key = _state_key(message.payload)
            if state_key is not None:
                setting = await session.get(SystemSetting, state_key)
                value = {"message_id": telegram_message_id, "updated_at": utc_now().isoformat()}
                if setting is None:
                    session.add(SystemSetting(key=state_key, value=value))
                else:
                    setting.value = value
                    setting.updated_at = utc_now()
            change_ids = _change_ids(message.payload)
            if change_ids:
                await session.execute(
                    update(ScheduleChange)
                    .where(
                        ScheduleChange.id.in_(change_ids),
                        ScheduleChange.notified_at.is_(None),
                    )
                    .values(notified_at=utc_now())
                )
            if material_copy is not None:
                material = await session.get(Material, material_copy.material_id)
                if material is None:
                    raise ValueError("material disappeared after Telegram copy")
                evidence = dict(material.evidence or {})
                all_destination_ids = _merge_ints(
                    evidence.get("destination_message_ids"),
                    destination_ids,
                )
                all_source_ids = _merge_ints(
                    evidence.get("copied_source_message_ids"),
                    material_copy.source_message_ids,
                )
                evidence.update(
                    {
                        "destination_message_ids": all_destination_ids,
                        "copied_source_message_ids": all_source_ids,
                        "copied_count": len(all_source_ids),
                        "source_deletion_enabled": False,
                        "source_deleted": False,
                    }
                )
                material.destination_chat_id = _required_chat_id(message)
                if material.destination_message_id is None:
                    material.destination_message_id = telegram_message_id
                material.workflow_state = "completed"
                material.evidence = evidence
                material.updated_at = utc_now()
        return telegram_message_id

    def _authorize_schedule_only(self, message: OutboxClaim) -> None:
        payload = message.payload
        state_key = _state_key(payload)
        is_schedule_card = state_key is not None and state_key.startswith(
            "telegram.schedule_card."
        )
        is_schedule_change = bool(_change_ids(payload))
        thread_id = payload.get("message_thread_id") if isinstance(payload, dict) else None
        if (
            message.chat_id != self._settings.target_chat_id
            or thread_id != self._settings.schedule_topic_id
            or message.kind not in {"telegram.send_message", "telegram.edit_message"}
            or "reply_markup" in payload
            or not (is_schedule_card or is_schedule_change)
        ):
            raise ScheduleOnlyEnvelopeDenied(
                "schedule-only mode rejected a non-schedule Telegram envelope"
            )

    async def _authorize_private_recipient(self, chat_id: int | None) -> None:
        if chat_id is None or chat_id < 0:
            return
        if chat_id != self._settings.headman_user_id:
            raise PrivateRecipientAccessDenied("private recipient is not the current headman")
        target_chat_id = self._settings.target_chat_id
        if target_chat_id is None or target_chat_id >= 0 or self._membership_gateway is None:
            raise PrivateRecipientAccessDenied("headman membership cannot be verified")
        membership = await self._membership_gateway.current_membership(
            chat_id=target_chat_id,
            user_id=chat_id,
        )
        if not can_access_group_history(membership):
            raise PrivateRecipientAccessDenied("headman is not a current group member")


class _AttendanceMetadata:
    def __init__(self, lesson_id: int, starts_at: datetime) -> None:
        self.lesson_id = lesson_id
        self.starts_at = starts_at


class _MaterialCopyMetadata:
    def __init__(
        self,
        material_id: int,
        expected_count: int,
        source_message_ids: tuple[int, ...],
    ) -> None:
        self.material_id = material_id
        self.expected_count = expected_count
        self.source_message_ids = source_message_ids


def _attendance_metadata(payload: object) -> _AttendanceMetadata | None:
    if not isinstance(payload, dict):
        return None
    raw = payload.get("_attendance")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("attendance metadata must be an object")
    lesson_id = raw.get("lesson_id")
    starts_at = raw.get("starts_at")
    if not isinstance(lesson_id, int) or isinstance(lesson_id, bool):
        raise ValueError("attendance metadata has no lesson_id")
    if not isinstance(starts_at, str):
        raise ValueError("attendance metadata has no starts_at")
    try:
        parsed = datetime.fromisoformat(starts_at)
    except ValueError as exc:
        raise ValueError("attendance metadata has invalid starts_at") from exc
    if parsed.tzinfo is None:
        raise ValueError("attendance starts_at must be timezone-aware")
    return _AttendanceMetadata(lesson_id, parsed)


def _material_copy_metadata(payload: object) -> _MaterialCopyMetadata | None:
    if not isinstance(payload, dict):
        return None
    raw = payload.get("_material_copy")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("material copy metadata must be an object")
    material_id = raw.get("material_id")
    expected_count = raw.get("expected_count")
    source_message_ids = raw.get("source_message_ids")
    if not isinstance(material_id, int) or isinstance(material_id, bool) or material_id <= 0:
        raise ValueError("material copy metadata has no material_id")
    if (
        not isinstance(expected_count, int)
        or isinstance(expected_count, bool)
        or not 1 <= expected_count <= 100
    ):
        raise ValueError("material copy metadata has invalid expected_count")
    if not isinstance(source_message_ids, list) or len(source_message_ids) != expected_count:
        raise ValueError("material copy metadata has invalid source_message_ids")
    if not all(
        isinstance(item, int) and not isinstance(item, bool) and item > 0
        for item in source_message_ids
    ):
        raise ValueError("material copy metadata has invalid source message ID")
    if len(set(source_message_ids)) != len(source_message_ids):
        raise ValueError("material copy metadata has duplicate source message IDs")
    return _MaterialCopyMetadata(material_id, expected_count, tuple(source_message_ids))


def _evidence_ints(evidence: object, key: str) -> set[int]:
    if not isinstance(evidence, dict):
        return set()
    value = evidence.get(key)
    if not isinstance(value, list):
        return set()
    return {
        item
        for item in value
        if isinstance(item, int) and not isinstance(item, bool) and item > 0
    }


def _merge_ints(existing: object, additions: tuple[int, ...]) -> list[int]:
    values = (
        [
            item
            for item in existing
            if isinstance(item, int) and not isinstance(item, bool) and item > 0
        ]
        if isinstance(existing, list)
        else []
    )
    return list(dict.fromkeys([*values, *additions]))


def _state_key(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    value = payload.get("_state_key")
    if value is None:
        return None
    if not isinstance(value, str) or not value.startswith("telegram.") or len(value) > 128:
        raise ValueError("outbox state key is invalid")
    return value


def _change_ids(payload: object) -> tuple[int, ...]:
    if not isinstance(payload, dict):
        return ()
    value = payload.get("_schedule_change_ids")
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError("schedule change metadata is invalid")
    if not all(isinstance(item, int) and not isinstance(item, bool) for item in value):
        raise ValueError("schedule change ID is invalid")
    return tuple(value)


def _required_chat_id(message: OutboxClaim) -> int:
    if message.chat_id is None:
        raise ValueError("attendance envelope has no destination chat")
    return message.chat_id
