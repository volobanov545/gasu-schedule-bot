"""One-off, presentation-only weekend joke requested by the group owner."""

from collections.abc import Iterable
from datetime import date, time

from szs_hub.domain.schedule import Lesson

# The nearest free weekend verified against the public timetable on 29 October.
# This must not roll forward automatically and become a weekly fake lesson.
WEEKEND_PLAN_DAY = date(2026, 11, 1)
WEEKEND_PLAN = (
    ("09:00–10:00", "Поздравить мир с моим пробуждением"),
    ("12:00–13:00", "Изысканно отобедать"),
    ("14:00–17:00", "Просто сиять"),
    ("18:00–19:00", "Вечернее унижение завистников"),
    ("20:00–23:00", "Царственно отдыхать"),
)


def is_weekend_plan_lesson(lesson: Lesson) -> bool:
    """Recognize only our view rows, which have no invented room or teacher."""

    return lesson.day == WEEKEND_PLAN_DAY and any(
        lesson.source_id == f"weekend-plan:{WEEKEND_PLAN_DAY}:{index}"
        and lesson.subject == activity
        for index, (_hours, activity) in enumerate(WEEKEND_PLAN)
    )


def weekend_plan_lessons(
    *,
    today: date,
    horizon_start: date,
    horizon_end: date,
    lesson_days: Iterable[date],
) -> tuple[Lesson, ...]:
    """Supply display-only rows; the university snapshot always stays unchanged."""

    if (
        today > WEEKEND_PLAN_DAY
        or not horizon_start <= WEEKEND_PLAN_DAY <= horizon_end
        or WEEKEND_PLAN_DAY in lesson_days
    ):
        return ()

    return tuple(
        Lesson(
            day=WEEKEND_PLAN_DAY,
            starts_at=time.fromisoformat(hours.split("–")[0]),
            ends_at=time.fromisoformat(hours.split("–")[1]),
            subject=activity,
            source_id=f"weekend-plan:{WEEKEND_PLAN_DAY}:{index}",
        )
        for index, (hours, activity) in enumerate(WEEKEND_PLAN)
    )
