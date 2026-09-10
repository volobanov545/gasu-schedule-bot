"""Durable application jobs for schedules, attendance cards, and summaries."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta, tzinfo
from datetime import timezone as fixed_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from szs_hub.attendance.service import AttendanceService, render_attendance_summary
from szs_hub.config import FeatureProfile, Settings
from szs_hub.domain.schedule import Lesson as DomainLesson
from szs_hub.jobs.queue import JobClaim, JobQueue, OutboxQueue
from szs_hub.jobs.worker import JobHandler
from szs_hub.retention import RetentionService
from szs_hub.schedule.coordinator import ScheduleAccess, ScheduleCoordinator, monday_for
from szs_hub.schedule.render import render_day_card, render_schedule_changes
from szs_hub.schedule.service import ScheduleFreshnessState
from szs_hub.schedule.ui import ScheduleView, render_attendance_card, schedule_keyboard
from szs_hub.storage.models import (
    AttendanceSession,
    Lesson,
    SystemSetting,
)


class ApplicationJobHandlers:
    """Registered handlers; every external Telegram effect goes through the outbox."""

    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        schedule: ScheduleCoordinator,
        jobs: JobQueue,
        outbox: OutboxQueue,
        clock: Callable[[], datetime],
    ) -> None:
        self._settings = settings
        self._sessions = session_factory
        self._schedule = schedule
        self._jobs = jobs
        self._outbox = outbox
        self._clock = clock
        self._timezone = _load_timezone(settings.timezone)

    @property
    def mapping(self) -> Mapping[str, JobHandler]:
        schedule_handlers: dict[str, JobHandler] = {
            "schedule.sync": self.schedule_sync,
            "schedule.evening": self.schedule_evening,
            "runtime.heartbeat": self.runtime_heartbeat,
        }
        if self._settings.feature_profile is FeatureProfile.SCHEDULE_ONLY:
            return schedule_handlers
        return {
            **schedule_handlers,
            "attendance.open": self.attendance_open,
            "attendance.summary": self.attendance_summary,
            "maintenance.minute": self.maintenance_minute,
            "maintenance.retention": self.maintenance_retention,
        }

    async def schedule_sync(self, _job: JobClaim) -> None:
        accesses = await self._schedule.current_and_next(force_refresh=True)
        local_today = self._clock().astimezone(self._timezone).date()
        for access in accesses:
            if access.schedule is None:
                continue
            if self._settings.feature_profile is FeatureProfile.FULL:
                await self._seed_attendance(access)
            if access.changes and access.change_ids:
                await self._publish_changes(access)
                affected = {
                    lesson.day
                    for change in access.changes
                    for lesson in (change.after or change.before,)
                    if lesson is not None
                }
                tomorrow = local_today + timedelta(days=1)
                if tomorrow in affected:
                    await self._publish_day(access, tomorrow, only_if_existing=True)

    async def schedule_evening(self, _job: JobClaim) -> None:
        tomorrow = self._clock().astimezone(self._timezone).date() + timedelta(days=1)
        access = await self._schedule.get_week(monday_for(tomorrow))
        if access.schedule is not None:
            await self._publish_day(access, tomorrow, only_if_existing=False)

    async def attendance_open(self, job: JobClaim) -> None:
        lesson_id = _payload_int(job.payload, "lesson_id")
        async with self._sessions() as session:
            lesson = await session.get(Lesson, lesson_id)
        if lesson is None:
            return
        starts_at = datetime.combine(
            lesson.day,
            lesson.starts_at,
            tzinfo=self._timezone,
        )
        now = self._clock().astimezone(self._timezone)
        closes_at = starts_at + timedelta(minutes=self._settings.attendance_window_minutes)
        if now >= closes_at:
            return
        domain = _domain_lesson(lesson)
        text = render_attendance_card(
            lesson=domain,
            starts_at=starts_at,
            reaction=self._settings.attendance_reaction,
        )
        await self._outbox.enqueue(
            kind="telegram.send_message",
            idempotency_key=f"attendance-card:{lesson.id}",
            chat_id=_required(self._settings.target_chat_id, "target_chat_id"),
            payload={
                "text": text,
                "message_thread_id": _required(
                    self._settings.schedule_topic_id,
                    "schedule_topic_id",
                ),
                "_attendance": {
                    "lesson_id": lesson.id,
                    "starts_at": starts_at.isoformat(),
                },
                "_state_key": f"telegram.attendance.{lesson.id}",
            },
            max_attempts=8,
        )

    async def attendance_summary(self, job: JobClaim) -> None:
        session_id = _payload_int(job.payload, "session_id")
        async with self._sessions() as session:
            attendance = await session.get(AttendanceSession, session_id)
            if attendance is None:
                return
            lesson = await session.get(Lesson, attendance.lesson_id)
            if lesson is None:
                return
            summary = await AttendanceService(session).summary(session_id=session_id)
            state_key = f"telegram.attendance_summary.{session_id}"
            existing_message_id = await _state_message_id(session, state_key)

        starts_at = datetime.combine(lesson.day, lesson.starts_at, tzinfo=self._timezone)
        text = render_attendance_summary(
            lesson_title=lesson.subject,
            starts_at=starts_at,
            summary=summary,
        )
        payload: dict[str, object] = {"text": text, "_state_key": state_key}
        kind = "telegram.send_message"
        if existing_message_id is not None:
            kind = "telegram.edit_message"
            payload["message_id"] = existing_message_id
        digest = hashlib.sha256(text.encode()).hexdigest()[:16]
        await self._outbox.enqueue(
            kind=kind,
            idempotency_key=f"attendance-summary:{session_id}:{digest}",
            chat_id=_required(self._settings.headman_user_id, "headman_user_id"),
            payload=payload,
            max_attempts=8,
        )

    async def maintenance_minute(self, _job: JobClaim) -> None:
        now = self._clock()
        async with self._sessions() as session, session.begin():
            await AttendanceService(session).close_due(now=now)
        await self.runtime_heartbeat(_job)

    async def runtime_heartbeat(self, _job: JobClaim) -> None:
        now = self._clock()
        async with self._sessions() as session, session.begin():
            heartbeat = await session.get(SystemSetting, "runtime.heartbeat")
            value = {"at": now.isoformat()}
            if heartbeat is None:
                session.add(SystemSetting(key="runtime.heartbeat", value=value))
            else:
                heartbeat.value = value
                heartbeat.updated_at = now

    async def maintenance_retention(self, _job: JobClaim) -> None:
        """Drain a bounded daily privacy-retention backlog."""

        service = RetentionService(self._sessions)
        for _ in range(10):
            result = await service.run_batch(now=self._clock(), limit=500)
            if not result.may_have_more:
                return

    async def _seed_attendance(self, access: ScheduleAccess) -> None:
        assert access.schedule is not None
        now = self._clock()
        async with self._sessions() as session:
            lessons = tuple(
                await session.scalars(
                    select(Lesson).where(Lesson.snapshot_id == access.schedule.snapshot_id)
                )
            )
        for lesson in lessons:
            starts_at = datetime.combine(
                lesson.day,
                lesson.starts_at,
                tzinfo=self._timezone,
            ).astimezone(UTC)
            closes_at = starts_at + timedelta(minutes=self._settings.attendance_window_minutes)
            if closes_at <= now:
                continue
            await self._jobs.enqueue(
                kind="attendance.open",
                idempotency_key=f"attendance-open:{lesson.id}",
                payload={"lesson_id": lesson.id},
                run_at=starts_at,
                max_attempts=8,
            )

    async def _publish_changes(self, access: ScheduleAccess) -> None:
        text = render_schedule_changes(access.changes, relative_label="в расписании")
        if not text:
            return
        ids = sorted(access.change_ids)
        await self._outbox.enqueue(
            kind="telegram.send_message",
            idempotency_key="schedule-changes:" + ":".join(str(item) for item in ids),
            chat_id=_required(self._settings.target_chat_id, "target_chat_id"),
            payload={
                "text": text,
                "message_thread_id": _required(
                    self._settings.schedule_topic_id,
                    "schedule_topic_id",
                ),
                "_schedule_change_ids": ids,
            },
            max_attempts=8,
        )

    async def _publish_day(
        self,
        access: ScheduleAccess,
        day: date,
        *,
        only_if_existing: bool,
    ) -> None:
        assert access.schedule is not None
        state_key = f"telegram.schedule_card.{day.isoformat()}"
        async with self._sessions() as session:
            existing_message_id = await _state_message_id(session, state_key)
        if only_if_existing and existing_message_id is None:
            return

        lessons = tuple(
            lesson for lesson in access.schedule.snapshot.lessons if lesson.day == day
        )
        text = render_day_card(day, lessons, relative_label="Завтра")
        if access.schedule.freshness.state is not ScheduleFreshnessState.FRESH:
            text += "\n\n<i>Показана последняя подтверждённая версия; источник не обновлён.</i>"
        payload: dict[str, object] = {
            "text": text,
            "message_thread_id": _required(
                self._settings.schedule_topic_id,
                "schedule_topic_id",
            ),
            "_state_key": state_key,
        }
        if self._settings.feature_profile is FeatureProfile.FULL:
            payload["reply_markup"] = schedule_keyboard(
                active=ScheduleView.TOMORROW
            ).model_dump(mode="json", exclude_none=True)
        kind = "telegram.send_message"
        if existing_message_id is not None:
            kind = "telegram.edit_message"
            payload["message_id"] = existing_message_id
        digest = hashlib.sha256(text.encode()).hexdigest()[:16]
        await self._outbox.enqueue(
            kind=kind,
            idempotency_key=f"schedule-card:{day.isoformat()}:{digest}",
            chat_id=_required(self._settings.target_chat_id, "target_chat_id"),
            payload=payload,
            max_attempts=8,
        )


async def _state_message_id(session: AsyncSession, key: str) -> int | None:
    setting = await session.get(SystemSetting, key)
    if setting is None or not isinstance(setting.value, dict):
        return None
    value = setting.value.get("message_id")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _domain_lesson(lesson: Lesson) -> DomainLesson:
    return DomainLesson(
        day=lesson.day,
        starts_at=lesson.starts_at,
        ends_at=lesson.ends_at,
        subject=lesson.subject,
        lesson_type=lesson.lesson_type,
        teacher=lesson.teacher,
        room=lesson.room,
        building=lesson.building,
        subgroup=lesson.subgroup,
        source_id=lesson.source_id,
    )


def _payload_int(payload: object, key: str) -> int:
    if not isinstance(payload, Mapping):
        raise ValueError("job payload must be an object")
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"job payload has no {key}")
    return value


def _required(value: int | None, name: str) -> int:
    if value is None:
        raise ValueError(f"required setting is missing: {name}")
    return value


def _load_timezone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name == "Europe/Moscow":
            return fixed_timezone(timedelta(hours=3), name=name)
        raise
