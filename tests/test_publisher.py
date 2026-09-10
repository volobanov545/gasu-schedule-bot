from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from szs_hub.config import Settings
from szs_hub.publisher import (
    GitHubWorkflowBridge,
    decode_schedule_card,
    dispatch_tomorrow,
    encode_schedule_card,
    publish_dispatched,
    publish_tomorrow,
)
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


class EmptySource:
    async def fetch_bootstrap(self) -> SchedulePageBootstrap:
        return SchedulePageBootstrap(current_week_number=5, groups=("СЗС-3",))

    async def fetch_group(self, group_key: str) -> WeeklySchedule:
        return WeeklySchedule(group_key=group_key, lessons=())


class FakeBridge:
    def __init__(self) -> None:
        self.dispatched: tuple[str, str, str, str] | None = None

    async def dispatch(
        self,
        *,
        repository: str,
        token: str,
        text_b64: str,
        group_key: str,
    ) -> None:
        self.dispatched = (repository, token, text_b64, group_key)


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [200, 204])
async def test_github_bridge_accepts_successful_dispatch(status_code: int) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/receive-schedule.yml/dispatches")
        return httpx.Response(status_code)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        bridge = GitHubWorkflowBridge(client)
        await bridge.dispatch(
            repository="owner/repository",
            token="fixture-value",  # noqa: S106
            text_b64="ZmFrZQ==",
            group_key="СЗС-3",
        )


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


@pytest.mark.asyncio
async def test_gitverse_bridge_dispatches_card_without_telegram_secret() -> None:
    bridge = FakeBridge()
    settings = Settings(spbgasu_group_id="СЗС-3", _env_file=None)

    await dispatch_tomorrow(
        settings,
        github_token="fixture-value",  # noqa: S106
        github_repository="owner/repository",
        source=FakeSource(),
        bridge=bridge,
        clock=lambda: datetime(2026, 8, 30, 12, tzinfo=UTC),
    )

    assert bridge.dispatched is not None
    repository, token, text_b64, group_key = bridge.dispatched
    assert (repository, token, group_key) == (
        "owner/repository",
        "fixture-value",
        "СЗС-3",
    )
    text = decode_schedule_card(text_b64)
    assert "Геодезия" in text
    assert "Иванов" not in text


@pytest.mark.asyncio
async def test_gitverse_bridge_dispatches_empty_day_card() -> None:
    bridge = FakeBridge()
    settings = Settings(spbgasu_group_id="СЗС-3", _env_file=None)

    await dispatch_tomorrow(
        settings,
        github_token="fixture-value",  # noqa: S106
        github_repository="owner/repository",
        source=EmptySource(),
        bridge=bridge,
        clock=lambda: datetime(2026, 9, 11, 12, tzinfo=UTC),
    )

    assert bridge.dispatched is not None
    text = decode_schedule_card(bridge.dispatched[2])
    assert "суббота, 12 сентября" in text
    assert "Пар нет." in text


@pytest.mark.asyncio
async def test_github_receiver_validates_group_and_sends_card() -> None:
    destination = FakeDestination()
    settings = Settings(
        telegram_bot_token=SecretStr("123456:token"),
        target_chat_id=-1001,
        schedule_topic_id=42,
        spbgasu_group_id="СЗС-3",
        _env_file=None,
    )
    text = (
        "📅 <b>Завтра</b> · понедельник, 31 августа\n\nПар нет.\n\n"
        '<a href="https://rasp.spbgasu.ru/">Источник: СПбГАСУ</a>'
    )

    message_id = await publish_dispatched(
        settings,
        text_b64=encode_schedule_card(text),
        group_key="сзс-3",
        destination=destination,
    )

    assert message_id == 77
    assert destination.sent == (-1001, 42, text)

    with pytest.raises(ValueError, match="does not match"):
        await publish_dispatched(
            settings,
            text_b64=encode_schedule_card(text),
            group_key="ДРУГАЯ-ГРУППА",
            destination=destination,
        )


def test_github_workflow_has_manual_run_and_four_secrets() -> None:
    workflow = (
        Path(__file__).parents[1] / ".github" / "workflows" / "publish-schedule.yml"
    ).read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert "schedule:" not in workflow
    for name in (
        "TELEGRAM_BOT_TOKEN",
        "TARGET_CHAT_ID",
        "SCHEDULE_TOPIC_ID",
        "SPBGASU_GROUP_ID",
    ):
        assert f"secrets.{name}" in workflow
    assert "szs-hub publish-tomorrow" in workflow


def test_ci_bridge_workflows_keep_telegram_token_out_of_gitverse() -> None:
    root = Path(__file__).parents[1]
    gitverse = (root / ".gitverse/workflows/fetch-schedule.yml").read_text(
        encoding="utf-8"
    )
    receiver = (root / ".github/workflows/receive-schedule.yml").read_text(
        encoding="utf-8"
    )

    assert 'cron: "30 17 * * *"' in gitverse
    assert "secrets.BRIDGE_GH_TOKEN" in gitverse
    assert "szs-hub dispatch-tomorrow" in gitverse
    assert "TELEGRAM_BOT_TOKEN" not in gitverse
    assert "inputs.text_b64" in receiver
    assert "secrets.TELEGRAM_BOT_TOKEN" in receiver
    assert "szs-hub publish-dispatched" in receiver
