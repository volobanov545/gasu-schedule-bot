from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from szs_hub.config import Settings
from szs_hub.domain.schedule import Lesson
from szs_hub.publisher import (
    GitHubWorkflowBridge,
    TelegramBotApiDestination,
    dispatch_tomorrow,
    encode_schedule_card,
    publish_dispatched,
    publish_due_reminder,
    publish_tomorrow,
)
from szs_hub.schedule.ci import (
    ScheduleDeliveryState,
    ScheduleEnvelope,
    decode_schedule_envelope,
    encode_schedule_envelope,
    load_delivery_state,
    save_delivery_state,
)
from szs_hub.schedule.spbgasu import (
    DEFAULT_BELL_SCHEDULE,
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
        self.sent_calls: list[tuple[int, int, str]] = []
        self.rich_sent: list[tuple[int, int, str, str, bool]] = []

    async def send(self, *, chat_id: int, topic_id: int, text: str) -> int:
        self.sent = (chat_id, topic_id, text)
        self.sent_calls.append(self.sent)
        return 77

    async def send_rich(
        self,
        *,
        chat_id: int,
        topic_id: int,
        rich_html: str,
        fallback_html: str,
        silent: bool,
    ) -> int:
        self.rich_sent.append((chat_id, topic_id, rich_html, fallback_html, silent))
        return 78


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


class FailingDestination:
    async def send(self, *, chat_id: int, topic_id: int, text: str) -> int:
        raise RuntimeError("Telegram is unavailable")


def _reminder_settings() -> Settings:
    return Settings(
        telegram_bot_token=SecretStr("123456:token"),
        target_chat_id=-1001,
        schedule_topic_id=42,
        spbgasu_group_id="3-СУЗСс-3",
        timezone="Europe/Moscow",
        _env_file=None,
    )


def _reminder_envelope() -> ScheduleEnvelope:
    return ScheduleEnvelope(
        group_key="3-СУЗСс-3",
        fetched_at=datetime(2026, 9, 11, 3, 0, tzinfo=UTC),
        horizon_start=date(2026, 9, 7),
        horizon_end=date(2026, 9, 20),
        lessons=(
            Lesson(
                day=date(2026, 9, 11),
                starts_at=time(10, 45),
                ends_at=time(12, 15),
                subject="Геодезия",
                room="407",
                building="2",
                source_id="lesson-1",
            ),
        ),
    )


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
    assert "Источник" not in text
    assert "rasp.spbgasu.ru" not in text


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
    envelope = decode_schedule_envelope(text_b64)
    assert envelope.group_key == "СЗС-3"
    assert [lesson.subject for lesson in envelope.lessons] == ["Геодезия"]
    assert all(lesson.teacher is None for lesson in envelope.lessons)


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
    envelope = decode_schedule_envelope(bridge.dispatched[2])
    assert envelope.horizon_start.isoformat() == "2026-09-07"
    assert envelope.horizon_end.isoformat() == "2026-09-20"
    assert envelope.lessons == ()


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
    text = "📅 <b>Завтра</b> · понедельник, 31 августа\n\nПар нет."

    message_ids = await publish_dispatched(
        settings,
        text_b64=encode_schedule_card(text),
        group_key="сзс-3",
        destination=destination,
    )

    assert message_ids == (77,)
    assert destination.sent == (-1001, 42, text)

    with pytest.raises(ValueError, match="does not match"):
        await publish_dispatched(
            settings,
            text_b64=encode_schedule_card(text),
            group_key="ДРУГАЯ-ГРУППА",
            destination=destination,
        )


@pytest.mark.asyncio
async def test_github_receiver_sends_forced_rich_digest_and_persists_state(
    tmp_path: Path,
) -> None:
    bridge = FakeBridge()
    source = FakeSource()
    settings = Settings(
        telegram_bot_token=SecretStr("123456:token"),
        target_chat_id=-1001,
        schedule_topic_id=42,
        spbgasu_group_id="СЗС-3",
        _env_file=None,
    )
    await dispatch_tomorrow(
        settings,
        github_token="fixture-value",  # noqa: S106
        github_repository="owner/repository",
        source=source,
        bridge=bridge,
        force_digest=True,
        clock=lambda: datetime(2026, 8, 30, 12, tzinfo=UTC),
    )
    assert bridge.dispatched is not None
    destination = FakeDestination()
    state_path = tmp_path / "state.json"

    message_ids = await publish_dispatched(
        settings,
        text_b64=bridge.dispatched[2],
        group_key="СЗС-3",
        destination=destination,
        state_path=state_path,
        clock=lambda: datetime(2026, 8, 30, 12, tzinfo=UTC),
    )

    assert message_ids == (78,)
    assert state_path.is_file()
    assert len(destination.rich_sent) == 1
    assert "<h1>📅 Сегодня" in destination.rich_sent[0][2]
    assert "<details><summary>Неделя" in destination.rich_sent[0][2]
    assert all(
        "Источник" not in rendered and "rasp.spbgasu.ru" not in rendered
        for rendered in destination.rich_sent[0][2:4]
    )


@pytest.mark.asyncio
async def test_forced_digest_rebaselines_without_false_added_changes(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    current = replace(_reminder_envelope(), force_digest=True)
    previous = replace(
        current,
        fetched_at=current.fetched_at - timedelta(hours=1),
        lessons=(),
        force_digest=False,
    )
    save_delivery_state(state_path, ScheduleDeliveryState(previous=previous))
    destination = FakeDestination()

    message_ids = await publish_dispatched(
        _reminder_settings(),
        text_b64=encode_schedule_envelope(current),
        group_key=current.group_key,
        destination=destination,
        state_path=state_path,
        clock=lambda: datetime(2026, 9, 11, 12, tzinfo=UTC),
    )

    assert message_ids == (78,)
    assert len(destination.rich_sent) == 1
    assert "Расписание изменилось" not in destination.rich_sent[0][2]
    assert "📅 Сегодня" in destination.rich_sent[0][2]
    assert load_delivery_state(state_path).previous == current


@pytest.mark.asyncio
async def test_receiver_ignores_an_out_of_order_older_snapshot(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    current = _reminder_envelope()
    saved = replace(current, fetched_at=current.fetched_at + timedelta(hours=1))
    initial = ScheduleDeliveryState(previous=saved)
    save_delivery_state(state_path, initial)
    destination = FakeDestination()

    message_ids = await publish_dispatched(
        _reminder_settings(),
        text_b64=encode_schedule_envelope(current),
        group_key=current.group_key,
        destination=destination,
        state_path=state_path,
        clock=lambda: datetime(2026, 9, 11, 12, tzinfo=UTC),
    )

    assert message_ids == ()
    assert destination.sent_calls == []
    assert destination.rich_sent == []
    assert load_delivery_state(state_path) == initial


@pytest.mark.asyncio
async def test_due_reminder_converts_utc_to_moscow_routes_and_deduplicates(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.json"
    save_delivery_state(
        state_path,
        ScheduleDeliveryState(previous=_reminder_envelope()),
    )
    destination = FakeDestination()
    # 05:45 UTC is 08:45 Moscow: exactly two hours before the 10:45 class.
    def clock() -> datetime:
        return datetime(2026, 9, 11, 5, 45, tzinfo=UTC)

    message_ids = await publish_due_reminder(
        _reminder_settings(),
        destination=destination,
        state_path=state_path,
        clock=clock,
    )

    assert message_ids == (77,)
    assert len(destination.sent_calls) == 1
    chat_id, topic_id, text = destination.sent_calls[0]
    assert (chat_id, topic_id) == (-1001, 42)
    assert "Первая пара через 2 часа" in text
    assert "10:45" in text
    assert load_delivery_state(state_path).sent_reminders == (
        "first:2026-09-11:10:45:00",
    )

    repeated = await publish_due_reminder(
        _reminder_settings(),
        destination=destination,
        state_path=state_path,
        clock=clock,
    )

    assert repeated == ()
    assert len(destination.sent_calls) == 1


@pytest.mark.asyncio
async def test_failed_due_reminder_does_not_persist_its_marker(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    initial = ScheduleDeliveryState(
        previous=_reminder_envelope(),
        sent_reminders=("older-marker",),
    )
    save_delivery_state(state_path, initial)

    with pytest.raises(RuntimeError, match="Telegram is unavailable"):
        await publish_due_reminder(
            _reminder_settings(),
            destination=FailingDestination(),
            state_path=state_path,
            clock=lambda: datetime(2026, 9, 11, 5, 45, tzinfo=UTC),
        )

    assert load_delivery_state(state_path) == initial


@pytest.mark.asyncio
async def test_rich_destination_uses_new_api_and_classic_fallback() -> None:
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/sendRichMessage"):
            return httpx.Response(400, json={"ok": False, "description": "unsupported"})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 91}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        destination = TelegramBotApiDestination("123456:token", client)
        message_id = await destination.send_rich(
            chat_id=-1001,
            topic_id=42,
            rich_html="<h1>Завтра</h1>",
            fallback_html="<b>Завтра</b>",
            silent=True,
        )

    assert message_id == 91
    assert seen == ["/bot123456:token/sendRichMessage", "/bot123456:token/sendMessage"]


def test_active_github_workflows_do_not_contain_legacy_source_link() -> None:
    workflow_dir = Path(__file__).parents[1] / ".github" / "workflows"
    workflows = tuple(workflow_dir.glob("*.yml"))

    assert workflows
    for workflow in workflows:
        content = workflow.read_text(encoding="utf-8")
        assert "Источник" not in content
        assert "rasp.spbgasu.ru" not in content


def test_reminder_crons_cover_every_first_class_and_possible_transition() -> None:
    workflow = (
        Path(__file__).parents[1] / ".github" / "workflows" / "remind-schedule.yml"
    ).read_text(encoding="utf-8")
    cron_matches = re.findall(r'- cron: "(\d+) (\d+) \* \* \*"', workflow)
    actual = {(int(hour), int(minute)) for minute, hour in cron_matches}

    moscow = timezone(timedelta(hours=3), name="Europe/Moscow")
    anchor = date(2026, 9, 11)

    def utc_moment(value: time, *, before: timedelta) -> tuple[int, int]:
        local = datetime.combine(anchor, value, tzinfo=moscow) - before
        utc = local.astimezone(UTC)
        return utc.hour, utc.minute

    slots = tuple(DEFAULT_BELL_SCHEDULE.values())
    expected = {
        utc_moment(starts_at, before=timedelta(hours=2))
        for starts_at, _ends_at in slots
    }
    expected.update(
        utc_moment(ends_at, before=timedelta(minutes=15))
        for _starts_at, ends_at in slots[:-1]
    )

    assert actual == expected


def test_ci_bridge_workflows_keep_telegram_token_out_of_gitverse() -> None:
    root = Path(__file__).parents[1]
    gitverse = (root / ".gitverse/workflows/fetch-schedule.yml").read_text(
        encoding="utf-8"
    )
    receiver = (root / ".github/workflows/receive-schedule.yml").read_text(
        encoding="utf-8"
    )
    reminder = (root / ".github/workflows/remind-schedule.yml").read_text(
        encoding="utf-8"
    )

    for cron in ('50 3', '20 9', '20 14', '30 17'):
        assert f'cron: "{cron} * * *"' in gitverse
    assert "secrets.BRIDGE_GH_TOKEN" in gitverse
    assert "github-server-url: https://github.com" in gitverse
    assert "repository: volobanov545/gasu-schedule-bot" in gitverse
    assert "szs-hub dispatch-tomorrow" in gitverse
    assert "TELEGRAM_BOT_TOKEN" not in gitverse
    assert "inputs.text_b64" in receiver
    assert "actions/cache/restore@v4" in receiver
    assert "actions/cache/save@v4" in receiver
    assert "secrets.TELEGRAM_BOT_TOKEN" in receiver
    assert "szs-hub publish-dispatched" in receiver
    for state_consumer in (receiver, reminder):
        assert "group: szs-schedule-state" in state_consumer
        assert "queue: max" in state_consumer
        assert "cancel-in-progress: false" in state_consumer
        assert "key: schedule-state-v2-${{ github.run_id }}-${{ github.run_attempt }}" in (
            state_consumer
        )
        assert "schedule-state-v2-" in state_consumer
