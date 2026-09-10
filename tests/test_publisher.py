from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr

from szs_hub.config import Settings
from szs_hub.publisher import publish_tomorrow
from szs_hub.schedule.spbgasu import (
    SchedulePageBootstrap,
    WeeklyLesson,
    WeeklySchedule,
    WeekParity,
)


class FakeSource:
    async def fetch_bootstrap(self) -> SchedulePageBootstrap:
        return SchedulePageBootstrap(current_week_number=5, groups=("СЗС-3",))

    async def fetch_group(self, group_key: str) -> WeeklySchedule:
        assert group_key == "СЗС-3"
        return WeeklySchedule(
            group_key=group_key,
            lessons=(
                WeeklyLesson(
                    weekday=0,
                    slot=1,
                    parity=WeekParity.DENOMINATOR,
                    subject="Геодезия (л.)",
                    group="СЗС-3",
                    auditorium="407/1",
                    professor="Иванов И. И.",
                    source_date=None,
                ),
            ),
        )


class FakeDestination:
    def __init__(self) -> None:
        self.sent: tuple[int, int, str] | None = None

    async def send(self, *, chat_id: int, topic_id: int, text: str) -> int:
        self.sent = (chat_id, topic_id, text)
        return 77


@pytest.mark.asyncio
async def test_ci_publisher_sends_minimal_tomorrow_card_without_teacher() -> None:
    destination = FakeDestination()
    settings = Settings(
        telegram_bot_token=SecretStr("123456:token"),
        target_chat_id=-1001,
        schedule_topic_id=42,
        spbgasu_group_id="СЗС-3",
        _env_file=None,
    )

    message_id = await publish_tomorrow(
        settings,
        source=FakeSource(),
        destination=destination,
        clock=lambda: datetime(2026, 8, 30, 12, tzinfo=UTC),
    )

    assert message_id == 77
    assert destination.sent is not None
    chat_id, topic_id, text = destination.sent
    assert (chat_id, topic_id) == (-1001, 42)
    assert "Геодезия" in text
    assert "Иванов" not in text
    assert "Источник: СПбГАСУ" in text


@pytest.mark.asyncio
async def test_ci_publisher_fails_before_network_without_required_secret() -> None:
    with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
        await publish_tomorrow(
            Settings(_env_file=None),
            source=FakeSource(),
            destination=FakeDestination(),
        )


def test_github_workflow_has_schedule_manual_run_and_four_secrets() -> None:
    workflow = (
        Path(__file__).parents[1] / ".github" / "workflows" / "publish-schedule.yml"
    ).read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert 'cron: "30 17 * * *"' in workflow
    for name in (
        "TELEGRAM_BOT_TOKEN",
        "TARGET_CHAT_ID",
        "SCHEDULE_TOPIC_ID",
        "SPBGASU_GROUP_ID",
    ):
        assert f"secrets.{name}" in workflow
    assert "szs-hub publish-tomorrow" in workflow
