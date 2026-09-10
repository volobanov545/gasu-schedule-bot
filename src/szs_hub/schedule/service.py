"""Durable, fail-stale synchronization for official SPbGASU schedules."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from szs_hub.domain.schedule import (
    Lesson,
    diff_schedules,
)
from szs_hub.domain.schedule import (
    ScheduleChange as DomainScheduleChange,
)
from szs_hub.domain.schedule import (
    ScheduleSnapshot as DomainScheduleSnapshot,
)
from szs_hub.schedule.spbgasu import (
    SpbGasuError,
    SpbGasuGroupNotFoundError,
    SpbGasuProtocolError,
    SpbGasuUnavailableError,
    WeeklySchedule,
    WeekParity,
    materialize_week,
)
from szs_hub.storage.models import (
    Lesson as StoredLesson,
)
from szs_hub.storage.models import (
    ScheduleChange as StoredScheduleChange,
)
from szs_hub.storage.models import (
    ScheduleSnapshot as StoredScheduleSnapshot,
)
from szs_hub.storage.models import (
    SystemSetting,
)

type Clock = Callable[[], datetime]

_SOURCE = "spbgasu"
_CONFIRMATION_SCHEMA = "szs_hub.schedule_confirmation.v1"
_SNAPSHOT_SCHEMA = "szs_hub.schedule_snapshot.v1"


class ScheduleClient(Protocol):
    async def fetch_group(self, group_key: str) -> WeeklySchedule: ...


class ScheduleFreshnessState(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


class ScheduleSourceFailure(StrEnum):
    UNAVAILABLE = "source_unavailable"
    GROUP_NOT_FOUND = "group_not_found"
    INVALID_RESPONSE = "invalid_response"


class ScheduleStateError(RuntimeError):
    """Raised when persisted schedule state violates its immutable contract."""


@dataclass(frozen=True, slots=True)
class ScheduleFreshness:
    state: ScheduleFreshnessState
    checked_at: datetime
    confirmed_at: datetime | None
    confirmation_age: timedelta | None
    fresh_for: timedelta
    source_failure: ScheduleSourceFailure | None = None

    @property
    def is_fresh(self) -> bool:
        return self.state is ScheduleFreshnessState.FRESH


@dataclass(frozen=True, slots=True)
class ConfirmedSchedule:
    snapshot_id: int
    snapshot: DomainScheduleSnapshot
    week_start: date
    parity: WeekParity | None
    freshness: ScheduleFreshness


@dataclass(frozen=True, slots=True)
class ScheduleSyncResult:
    schedule: ConfirmedSchedule | None
    freshness: ScheduleFreshness
    snapshot_created: bool
    changes: tuple[DomainScheduleChange, ...]
    change_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _StoredVersion:
    row: StoredScheduleSnapshot
    snapshot: DomainScheduleSnapshot
    lesson_ids: dict[str, int]


@dataclass(frozen=True, slots=True)
class _Confirmation:
    version: _StoredVersion | None
    confirmed_at: datetime | None
    parity: WeekParity | None
    last_attempt_failed: bool
    failure: ScheduleSourceFailure | None


class ScheduleSyncService:
    """Synchronize one requested week while retaining the last confirmed version.

    Snapshot rows are immutable and content addressed. A separate confirmation marker
    records successful no-change checks as well as failed source checks, so callers can
    distinguish recently verified data from stale fallback data after a restart.
    """

    def __init__(
        self,
        *,
        client: ScheduleClient,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock,
        fresh_for: timedelta = timedelta(minutes=30),
        retain_teacher_names: bool = True,
    ) -> None:
        if fresh_for <= timedelta(0):
            raise ValueError("schedule freshness window must be positive")
        self._client = client
        self._session_factory = session_factory
        self._clock = clock
        self._fresh_for = fresh_for
        self._retain_teacher_names = retain_teacher_names
        # The deployed architecture has one database writer. This lock also makes the
        # read-diff-confirm sequence deterministic when callers synchronize concurrently.
        self._sync_lock = asyncio.Lock()

    async def sync_week(
        self,
        *,
        group_key: str,
        week_start: date,
        parity: WeekParity,
    ) -> ScheduleSyncResult:
        group = _validate_request(group_key, week_start)
        async with self._sync_lock:
            try:
                weekly = await self._client.fetch_group(group)
                if weekly.group_key.strip() != group:
                    raise ScheduleStateError("schedule client returned a different group")
                lessons = materialize_week(weekly, monday=week_start, parity=parity)
                if not self._retain_teacher_names:
                    lessons = tuple(replace(lesson, teacher=None) for lesson in lessons)
            except SpbGasuError as exc:
                checked_at = _aware_utc(self._clock())
                failure = _classify_failure(exc)
                async with self._session_factory() as session, session.begin():
                    repository = _ScheduleRepository(session)
                    confirmation = await repository.load_confirmation(
                        source=_SOURCE,
                        group_key=group,
                        week_start=week_start,
                    )
                    await repository.record_failed_attempt(
                        source=_SOURCE,
                        group_key=group,
                        week_start=week_start,
                        checked_at=checked_at,
                        failure=failure,
                        confirmation=confirmation,
                    )
                freshness = _freshness(
                    checked_at=checked_at,
                    confirmed_at=confirmation.confirmed_at,
                    fresh_for=self._fresh_for,
                    source_failure=failure,
                    force_stale=True,
                )
                schedule = _confirmed_schedule(
                    confirmation,
                    week_start=week_start,
                    freshness=freshness,
                )
                return ScheduleSyncResult(schedule, freshness, False, (), ())

            checked_at = _aware_utc(self._clock())
            current_snapshot = DomainScheduleSnapshot(
                source=_SOURCE,
                group_key=group,
                fetched_at=checked_at,
                lessons=lessons,
            )

            async with self._session_factory() as session, session.begin():
                repository = _ScheduleRepository(session)
                previous = await repository.load_confirmation(
                    source=_SOURCE,
                    group_key=group,
                    week_start=week_start,
                )
                current, snapshot_created = await repository.store_snapshot(
                    current_snapshot,
                    week_start=week_start,
                    parity=parity,
                )
                changes: tuple[DomainScheduleChange, ...]
                if previous.version is None or previous.version.row.id == current.row.id:
                    changes = ()
                else:
                    changes = diff_schedules(previous.version.snapshot, current.snapshot)
                new_changes, change_ids = await repository.store_changes(
                    previous=previous.version,
                    current=current,
                    changes=changes,
                    detected_at=checked_at,
                )
                await repository.confirm(
                    source=_SOURCE,
                    group_key=group,
                    week_start=week_start,
                    checked_at=checked_at,
                    parity=parity,
                    version=current,
                )

            freshness = _freshness(
                checked_at=checked_at,
                confirmed_at=checked_at,
                fresh_for=self._fresh_for,
            )
            schedule = ConfirmedSchedule(
                snapshot_id=current.row.id,
                snapshot=current.snapshot,
                week_start=week_start,
                parity=parity,
                freshness=freshness,
            )
            return ScheduleSyncResult(
                schedule,
                freshness,
                snapshot_created,
                new_changes,
                change_ids,
            )

    async def load_latest_confirmed(
        self,
        *,
        group_key: str,
        week_start: date,
    ) -> ConfirmedSchedule | None:
        group = _validate_request(group_key, week_start)
        checked_at = _aware_utc(self._clock())
        async with self._session_factory() as session:
            confirmation = await _ScheduleRepository(session).load_confirmation(
                source=_SOURCE,
                group_key=group,
                week_start=week_start,
            )
        freshness = _freshness(
            checked_at=checked_at,
            confirmed_at=confirmation.confirmed_at,
            fresh_for=self._fresh_for,
            source_failure=confirmation.failure,
            force_stale=confirmation.last_attempt_failed,
        )
        return _confirmed_schedule(
            confirmation,
            week_start=week_start,
            freshness=freshness,
        )


class _ScheduleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load_confirmation(
        self,
        *,
        source: str,
        group_key: str,
        week_start: date,
    ) -> _Confirmation:
        marker = await self._session.get(
            SystemSetting,
            _confirmation_key(source, group_key, week_start),
        )
        if marker is not None:
            parsed = await self._load_marker(
                marker,
                source=source,
                group_key=group_key,
                week_start=week_start,
            )
            if parsed is not None:
                return parsed

        # Compatibility for rows written before confirmation markers existed and a
        # recovery path if a marker was manually removed. Only complete snapshots count.
        row = await self._session.scalar(
            select(StoredScheduleSnapshot)
            .where(
                StoredScheduleSnapshot.source == source,
                StoredScheduleSnapshot.group_key == group_key,
                StoredScheduleSnapshot.week_start == week_start,
                StoredScheduleSnapshot.is_complete.is_(True),
            )
            .order_by(StoredScheduleSnapshot.fetched_at.desc(), StoredScheduleSnapshot.id.desc())
            .limit(1)
        )
        if row is None:
            return _Confirmation(None, None, None, False, None)
        version = await self._load_version(row)
        return _Confirmation(version, row.fetched_at, _snapshot_parity(row), False, None)

    async def store_snapshot(
        self,
        snapshot: DomainScheduleSnapshot,
        *,
        week_start: date,
        parity: WeekParity,
    ) -> tuple[_StoredVersion, bool]:
        existing = await self._session.scalar(
            select(StoredScheduleSnapshot).where(
                StoredScheduleSnapshot.source == snapshot.source,
                StoredScheduleSnapshot.group_key == snapshot.group_key,
                StoredScheduleSnapshot.week_start == week_start,
                StoredScheduleSnapshot.content_hash == snapshot.content_hash,
            )
        )
        if existing is not None:
            if not existing.is_complete:
                raise ScheduleStateError("matching schedule snapshot is incomplete")
            return await self._load_version(existing), False

        row = StoredScheduleSnapshot(
            source=snapshot.source,
            group_key=snapshot.group_key,
            week_start=week_start,
            fetched_at=snapshot.fetched_at,
            content_hash=snapshot.content_hash,
            is_complete=True,
            raw_payload={
                "schema": _SNAPSHOT_SCHEMA,
                "parity": parity.value,
            },
        )
        self._session.add(row)
        await self._session.flush()

        lesson_ids: dict[str, int] = {}
        for lesson in snapshot.lessons:
            key = _lesson_key(lesson)
            if key in lesson_ids:
                raise ScheduleStateError("schedule contains duplicate lesson identities")
            stored = StoredLesson(
                snapshot_id=row.id,
                lesson_key=key,
                source_id=lesson.source_id,
                day=lesson.day,
                starts_at=lesson.starts_at,
                ends_at=lesson.ends_at,
                subject=lesson.subject,
                lesson_type=lesson.lesson_type,
                teacher=lesson.teacher,
                room=lesson.room,
                building=lesson.building,
                subgroup=lesson.subgroup,
            )
            self._session.add(stored)
            await self._session.flush()
            lesson_ids[key] = stored.id
        return _StoredVersion(row, snapshot, lesson_ids), True

    async def store_changes(
        self,
        *,
        previous: _StoredVersion | None,
        current: _StoredVersion,
        changes: Sequence[DomainScheduleChange],
        detected_at: datetime,
    ) -> tuple[tuple[DomainScheduleChange, ...], tuple[int, ...]]:
        if previous is None or not changes:
            return (), ()
        fingerprints = tuple(change.fingerprint for change in changes)
        existing = set(
            await self._session.scalars(
                select(StoredScheduleChange.fingerprint).where(
                    StoredScheduleChange.to_snapshot_id == current.row.id,
                    StoredScheduleChange.fingerprint.in_(fingerprints),
                )
            )
        )
        saved_changes: list[DomainScheduleChange] = []
        saved_ids: list[int] = []
        for change in changes:
            if change.fingerprint in existing:
                continue
            before_id = (
                previous.lesson_ids.get(_lesson_key(change.before)) if change.before else None
            )
            after_id = current.lesson_ids.get(_lesson_key(change.after)) if change.after else None
            if change.before is not None and before_id is None:
                raise ScheduleStateError("changed lesson is missing from previous snapshot")
            if change.after is not None and after_id is None:
                raise ScheduleStateError("changed lesson is missing from current snapshot")
            row = StoredScheduleChange(
                from_snapshot_id=previous.row.id,
                to_snapshot_id=current.row.id,
                before_lesson_id=before_id,
                after_lesson_id=after_id,
                kind=change.kind.value,
                fingerprint=change.fingerprint,
                detected_at=detected_at,
            )
            self._session.add(row)
            await self._session.flush()
            saved_changes.append(change)
            saved_ids.append(row.id)
            existing.add(change.fingerprint)
        return tuple(saved_changes), tuple(saved_ids)

    async def confirm(
        self,
        *,
        source: str,
        group_key: str,
        week_start: date,
        checked_at: datetime,
        parity: WeekParity,
        version: _StoredVersion,
    ) -> None:
        await self._write_marker(
            source=source,
            group_key=group_key,
            week_start=week_start,
            checked_at=checked_at,
            value={
                "schema": _CONFIRMATION_SCHEMA,
                "source": source,
                "group_key": group_key,
                "week_start": week_start.isoformat(),
                "snapshot_id": version.row.id,
                "confirmed_at": checked_at.isoformat(),
                "parity": parity.value,
                "last_attempt_status": "succeeded",
                "last_failure": None,
            },
        )

    async def record_failed_attempt(
        self,
        *,
        source: str,
        group_key: str,
        week_start: date,
        checked_at: datetime,
        failure: ScheduleSourceFailure,
        confirmation: _Confirmation,
    ) -> None:
        snapshot_id = confirmation.version.row.id if confirmation.version else None
        parity = confirmation.parity.value if confirmation.parity else None
        confirmed_at = (
            confirmation.confirmed_at.isoformat() if confirmation.confirmed_at else None
        )
        await self._write_marker(
            source=source,
            group_key=group_key,
            week_start=week_start,
            checked_at=checked_at,
            value={
                "schema": _CONFIRMATION_SCHEMA,
                "source": source,
                "group_key": group_key,
                "week_start": week_start.isoformat(),
                "snapshot_id": snapshot_id,
                "confirmed_at": confirmed_at,
                "parity": parity,
                "last_attempt_status": "failed",
                "last_failure": failure.value,
            },
        )

    async def _write_marker(
        self,
        *,
        source: str,
        group_key: str,
        week_start: date,
        checked_at: datetime,
        value: dict[str, object],
    ) -> None:
        key = _confirmation_key(source, group_key, week_start)
        marker = await self._session.get(SystemSetting, key)
        if marker is None:
            self._session.add(SystemSetting(key=key, value=value, updated_at=checked_at))
            return
        marker.value = value
        marker.updated_at = checked_at

    async def _load_marker(
        self,
        marker: SystemSetting,
        *,
        source: str,
        group_key: str,
        week_start: date,
    ) -> _Confirmation | None:
        value = marker.value
        if not isinstance(value, dict):
            return None
        if (
            value.get("schema") != _CONFIRMATION_SCHEMA
            or value.get("source") != source
            or value.get("group_key") != group_key
            or value.get("week_start") != week_start.isoformat()
        ):
            return None
        confirmed_at = _optional_datetime(value.get("confirmed_at"))
        parity = _optional_parity(value.get("parity"))
        last_attempt_failed = value.get("last_attempt_status") == "failed"
        failure = _optional_failure(value.get("last_failure"))
        raw_snapshot_id = value.get("snapshot_id")
        if raw_snapshot_id is None:
            return _Confirmation(None, confirmed_at, parity, last_attempt_failed, failure)
        if not isinstance(raw_snapshot_id, int) or isinstance(raw_snapshot_id, bool):
            return None
        row = await self._session.get(StoredScheduleSnapshot, raw_snapshot_id)
        if (
            row is None
            or not row.is_complete
            or row.source != source
            or row.group_key != group_key
            or row.week_start != week_start
        ):
            return None
        return _Confirmation(
            await self._load_version(row),
            confirmed_at,
            parity,
            last_attempt_failed,
            failure,
        )

    async def _load_version(self, row: StoredScheduleSnapshot) -> _StoredVersion:
        stored_lessons = tuple(
            await self._session.scalars(
                select(StoredLesson)
                .where(StoredLesson.snapshot_id == row.id)
                .order_by(
                    StoredLesson.day,
                    StoredLesson.starts_at,
                    StoredLesson.lesson_key,
                )
            )
        )
        domain_lessons = tuple(_to_domain_lesson(lesson) for lesson in stored_lessons)
        snapshot = DomainScheduleSnapshot(
            source=row.source,
            group_key=row.group_key,
            fetched_at=row.fetched_at,
            lessons=domain_lessons,
        )
        if snapshot.content_hash != row.content_hash:
            raise ScheduleStateError("persisted schedule snapshot hash does not match its lessons")
        lesson_ids = {lesson.lesson_key: lesson.id for lesson in stored_lessons}
        if len(lesson_ids) != len(stored_lessons):
            raise ScheduleStateError("persisted schedule contains duplicate lesson identities")
        return _StoredVersion(row, snapshot, lesson_ids)


def _validate_request(group_key: str, week_start: date) -> str:
    group = group_key.strip()
    if not group:
        raise ValueError("schedule group key cannot be empty")
    if week_start.weekday() != 0:
        raise ValueError("schedule week start must be a Monday")
    return group


def _to_domain_lesson(row: StoredLesson) -> Lesson:
    return Lesson(
        day=row.day,
        starts_at=row.starts_at,
        ends_at=row.ends_at,
        subject=row.subject,
        lesson_type=row.lesson_type,
        teacher=row.teacher,
        room=row.room,
        building=row.building,
        subgroup=row.subgroup,
        source_id=row.source_id,
    )


def _lesson_key(lesson: Lesson) -> str:
    payload = json.dumps(
        lesson.canonical(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _confirmation_key(source: str, group_key: str, week_start: date) -> str:
    identity = f"{source}\0{group_key}\0{week_start.isoformat()}"
    return f"schedule.confirmation.{hashlib.sha256(identity.encode()).hexdigest()}"


def _snapshot_parity(row: StoredScheduleSnapshot) -> WeekParity | None:
    payload = row.raw_payload
    if not isinstance(payload, dict):
        return None
    return _optional_parity(payload.get("parity"))


def _optional_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _optional_parity(value: object) -> WeekParity | None:
    if not isinstance(value, str):
        return None
    try:
        return WeekParity(value)
    except ValueError:
        return None


def _optional_failure(value: object) -> ScheduleSourceFailure | None:
    if not isinstance(value, str):
        return None
    try:
        return ScheduleSourceFailure(value)
    except ValueError:
        return None


def _classify_failure(error: SpbGasuError) -> ScheduleSourceFailure:
    if isinstance(error, SpbGasuGroupNotFoundError):
        return ScheduleSourceFailure.GROUP_NOT_FOUND
    if isinstance(error, SpbGasuProtocolError):
        return ScheduleSourceFailure.INVALID_RESPONSE
    if isinstance(error, SpbGasuUnavailableError):
        return ScheduleSourceFailure.UNAVAILABLE
    return ScheduleSourceFailure.UNAVAILABLE


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("schedule clock must return a timezone-aware timestamp")
    return value.astimezone(UTC)


def _freshness(
    *,
    checked_at: datetime,
    confirmed_at: datetime | None,
    fresh_for: timedelta,
    source_failure: ScheduleSourceFailure | None = None,
    force_stale: bool = False,
) -> ScheduleFreshness:
    if confirmed_at is None:
        return ScheduleFreshness(
            ScheduleFreshnessState.UNAVAILABLE,
            checked_at,
            None,
            None,
            fresh_for,
            source_failure,
        )
    age = checked_at - confirmed_at
    state = (
        ScheduleFreshnessState.FRESH
        if not force_stale and timedelta(0) <= age <= fresh_for
        else ScheduleFreshnessState.STALE
    )
    return ScheduleFreshness(state, checked_at, confirmed_at, age, fresh_for, source_failure)


def _confirmed_schedule(
    confirmation: _Confirmation,
    *,
    week_start: date,
    freshness: ScheduleFreshness,
) -> ConfirmedSchedule | None:
    if confirmation.version is None:
        return None
    return ConfirmedSchedule(
        snapshot_id=confirmation.version.row.id,
        snapshot=confirmation.version.snapshot,
        week_start=week_start,
        parity=confirmation.parity,
        freshness=freshness,
    )
