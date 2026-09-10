"""Machine-readable health checks without exposing user data or secrets."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from szs_hub.storage.base import utc_now
from szs_hub.storage.models import (
    Job,
    OutboxMessage,
    ProcessedUpdate,
    ScheduleSnapshot,
    SystemSetting,
)


class HealthState(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class HealthReport:
    state: HealthState
    checked_at: datetime
    database_integrity: str
    last_schedule_fetch: datetime | None
    last_runtime_heartbeat: datetime | None
    last_telegram_probe: datetime | None
    telegram_probe_ok: bool | None
    last_backup_success: datetime | None
    recent_failed_updates: int
    failed_jobs: int
    failed_outbox: int
    reasons: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return self.state is not HealthState.FAIL

    def as_dict(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "checked_at": self.checked_at.isoformat(),
            "database_integrity": self.database_integrity,
            "last_schedule_fetch": (
                self.last_schedule_fetch.isoformat() if self.last_schedule_fetch else None
            ),
            "last_runtime_heartbeat": (
                self.last_runtime_heartbeat.isoformat()
                if self.last_runtime_heartbeat
                else None
            ),
            "last_telegram_probe": (
                self.last_telegram_probe.isoformat() if self.last_telegram_probe else None
            ),
            "telegram_probe_ok": self.telegram_probe_ok,
            "last_backup_success": (
                self.last_backup_success.isoformat() if self.last_backup_success else None
            ),
            "recent_failed_updates": self.recent_failed_updates,
            "failed_jobs": self.failed_jobs,
            "failed_outbox": self.failed_outbox,
            "reasons": list(self.reasons),
        }


async def build_health_report(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    expect_schedule: bool,
    schedule_stale_after: timedelta,
    expect_runtime: bool = False,
    runtime_stale_after: timedelta = timedelta(minutes=5),
    expect_telegram: bool = False,
    telegram_probe_stale_after: timedelta = timedelta(minutes=30),
    backup_status_path: Path | None = None,
    backup_stale_after: timedelta = timedelta(hours=36),
    now: datetime | None = None,
) -> HealthReport:
    """Check durable state; failures are summarized without exception messages."""

    checked_at = now or utc_now()
    if checked_at.tzinfo is None:
        raise ValueError("health-check time must be timezone-aware")
    try:
        async with session_factory() as session:
            integrity = await session.scalar(text("PRAGMA quick_check(1)"))
            last_schedule = await session.scalar(select(func.max(ScheduleSnapshot.fetched_at)))
            heartbeat_setting = await session.get(SystemSetting, "runtime.heartbeat")
            telegram_setting = await session.get(SystemSetting, "telegram.probe")
            failed_updates = await session.scalar(
                select(func.count())
                .select_from(ProcessedUpdate)
                .where(
                    ProcessedUpdate.outcome == "failed",
                    ProcessedUpdate.processed_at >= checked_at - timedelta(hours=24),
                )
            )
            failed_jobs = await session.scalar(
                select(func.count()).select_from(Job).where(Job.status == "failed")
            )
            failed_outbox = await session.scalar(
                select(func.count())
                .select_from(OutboxMessage)
                .where(OutboxMessage.status == "failed")
            )
    except Exception as exc:
        return HealthReport(
            state=HealthState.FAIL,
            checked_at=checked_at,
            database_integrity="unavailable",
            last_schedule_fetch=None,
            last_runtime_heartbeat=None,
            last_telegram_probe=None,
            telegram_probe_ok=None,
            last_backup_success=None,
            recent_failed_updates=0,
            failed_jobs=0,
            failed_outbox=0,
            reasons=(f"database:{type(exc).__name__}",),
        )

    last_heartbeat = _setting_timestamp(
        heartbeat_setting.value if heartbeat_setting is not None else None
    )
    last_telegram_probe, telegram_ok = _probe_status(
        telegram_setting.value if telegram_setting is not None else None
    )
    last_backup, backup_reason = _backup_timestamp(backup_status_path)
    reasons: list[str] = []
    if integrity != "ok":
        reasons.append("database_integrity")
    if expect_schedule:
        if last_schedule is None:
            reasons.append("schedule_missing")
        elif last_schedule < checked_at - schedule_stale_after:
            reasons.append("schedule_stale")
    if expect_runtime:
        if last_heartbeat is None:
            reasons.append("runtime_heartbeat_missing")
        elif last_heartbeat < checked_at - runtime_stale_after:
            reasons.append("runtime_heartbeat_stale")
    if expect_telegram:
        if last_telegram_probe is None:
            reasons.append("telegram_probe_missing")
        elif last_telegram_probe < checked_at - telegram_probe_stale_after:
            reasons.append("telegram_probe_stale")
        elif telegram_ok is not True:
            reasons.append("telegram_probe_failed")
    if backup_reason is not None:
        reasons.append(backup_reason)
    elif last_backup is not None and last_backup < checked_at - backup_stale_after:
        reasons.append("backup_stale")
    if int(failed_updates or 0):
        reasons.append("recent_update_failures")
    if int(failed_jobs or 0):
        reasons.append("failed_jobs")
    if int(failed_outbox or 0):
        reasons.append("failed_outbox")

    state = HealthState.OK
    fail_reasons = {
        "database_integrity",
        "runtime_heartbeat_missing",
        "runtime_heartbeat_stale",
        "telegram_probe_missing",
        "telegram_probe_stale",
        "telegram_probe_failed",
    }
    if reasons:
        state = (
            HealthState.FAIL
            if any(reason in fail_reasons for reason in reasons)
            else HealthState.DEGRADED
        )
    return HealthReport(
        state=state,
        checked_at=checked_at,
        database_integrity=str(integrity),
        last_schedule_fetch=last_schedule,
        last_runtime_heartbeat=last_heartbeat,
        last_telegram_probe=last_telegram_probe,
        telegram_probe_ok=telegram_ok,
        last_backup_success=last_backup,
        recent_failed_updates=int(failed_updates or 0),
        failed_jobs=int(failed_jobs or 0),
        failed_outbox=int(failed_outbox or 0),
        reasons=tuple(reasons),
    )


def _setting_timestamp(value: object) -> datetime | None:
    if not isinstance(value, dict):
        return None
    raw = value.get("at")
    return _parse_timestamp(raw)


def _probe_status(value: object) -> tuple[datetime | None, bool | None]:
    if not isinstance(value, dict):
        return None, None
    ok = value.get("ok")
    return _parse_timestamp(value.get("at")), ok if isinstance(ok, bool) else None


def _backup_timestamp(path: Path | None) -> tuple[datetime | None, str | None]:
    if path is None:
        return None, None
    try:
        candidate = path.expanduser().resolve()
        if not candidate.is_file() or candidate.stat().st_size > 256:
            return None, "backup_status_missing"
        timestamp = _parse_timestamp(candidate.read_text(encoding="utf-8").strip())
    except OSError:
        return None, "backup_status_unavailable"
    if timestamp is None:
        return None, "backup_status_invalid"
    return timestamp, None


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or len(value) > 128:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed
