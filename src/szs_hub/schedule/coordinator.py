"""Resolve official academic-week parity and serve fail-stale schedule views."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from szs_hub.domain.schedule import ScheduleChange
from szs_hub.schedule.service import (
    ConfirmedSchedule,
    ScheduleFreshnessState,
    ScheduleSyncService,
)
from szs_hub.schedule.spbgasu import (
    SchedulePageBootstrap,
    SpbGasuError,
    WeekParity,
    parity_for_week,
)


class BootstrapClient(Protocol):
    async def fetch_bootstrap(self) -> SchedulePageBootstrap: ...


@dataclass(frozen=True, slots=True)
class ScheduleAccess:
    schedule: ConfirmedSchedule | None
    refresh_attempted: bool
    bootstrap_available: bool
    changes: tuple[ScheduleChange, ...] = ()
    change_ids: tuple[int, ...] = ()


class ScheduleCoordinator:
    """Refresh only stale data and never replace a confirmed snapshot with guesses."""

    def __init__(
        self,
        *,
        group_key: str,
        bootstrap_client: BootstrapClient,
        sync_service: ScheduleSyncService,
        clock: Callable[[], datetime],
        timezone: str = "Europe/Moscow",
    ) -> None:
        if not group_key.strip():
            raise ValueError("schedule group key cannot be empty")
        self._group_key = group_key.strip()
        self._client = bootstrap_client
        self._service = sync_service
        self._clock = clock
        self._timezone: tzinfo
        try:
            self._timezone = ZoneInfo(timezone)
        except ZoneInfoNotFoundError:
            if timezone != "Europe/Moscow":
                raise
            from datetime import timezone as fixed_timezone

            self._timezone = fixed_timezone(timedelta(hours=3), name=timezone)

    async def get_week(self, week_start: date, *, force_refresh: bool = False) -> ScheduleAccess:
        if week_start.weekday() != 0:
            raise ValueError("schedule week must start on Monday")
        confirmed = await self._service.load_latest_confirmed(
            group_key=self._group_key,
            week_start=week_start,
        )
        if (
            not force_refresh
            and confirmed is not None
            and confirmed.freshness.state is ScheduleFreshnessState.FRESH
        ):
            return ScheduleAccess(confirmed, False, True)

        try:
            bootstrap = await self._client.fetch_bootstrap()
        except SpbGasuError:
            return ScheduleAccess(confirmed, True, False)

        today = self._clock().astimezone(self._timezone).date()
        current_monday = today - timedelta(days=today.weekday())
        parity = parity_for_week(
            current_week_number=bootstrap.current_week_number,
            current_monday=current_monday,
            target_monday=week_start,
        )
        result = await self._service.sync_week(
            group_key=self._group_key,
            week_start=week_start,
            parity=parity,
        )
        return ScheduleAccess(
            result.schedule,
            True,
            True,
            result.changes,
            result.change_ids,
        )

    async def current_and_next(self, *, force_refresh: bool = False) -> tuple[ScheduleAccess, ...]:
        today = self._clock().astimezone(self._timezone).date()
        monday = today - timedelta(days=today.weekday())
        return (
            await self.get_week(monday, force_refresh=force_refresh),
            await self.get_week(monday + timedelta(days=7), force_refresh=force_refresh),
        )


def monday_for(day: date) -> date:
    return day - timedelta(days=day.weekday())


def parity_label(parity: WeekParity | None) -> str:
    if parity is WeekParity.NUMERATOR:
        return "числитель"
    if parity is WeekParity.DENOMINATOR:
        return "знаменатель"
    return "чётность не подтверждена"
