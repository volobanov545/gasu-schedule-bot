"""Schedule acquisition, storage, diffing, and presentation."""

from importlib import import_module
from typing import TYPE_CHECKING, Any

from szs_hub.schedule.render import render_day_card, render_schedule_changes

if TYPE_CHECKING:
    from szs_hub.schedule.service import (
        ConfirmedSchedule,
        ScheduleFreshness,
        ScheduleFreshnessState,
        ScheduleSourceFailure,
        ScheduleStateError,
        ScheduleSyncResult,
        ScheduleSyncService,
    )

__all__ = [
    "ConfirmedSchedule",
    "ScheduleFreshness",
    "ScheduleFreshnessState",
    "ScheduleSourceFailure",
    "ScheduleStateError",
    "ScheduleSyncResult",
    "ScheduleSyncService",
    "render_day_card",
    "render_schedule_changes",
]


def __getattr__(name: str) -> Any:
    """Keep public service exports without loading a database for the CI parser."""

    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(".service", __name__), name)
    globals()[name] = value
    return value
