from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr

from szs_hub import cli, publisher
from szs_hub.config import Settings
from szs_hub.domain.schedule import Lesson
from szs_hub.schedule.ci import (
    ScheduleDeliveryState,
    ScheduleEnvelope,
    encode_schedule_envelope,
    load_delivery_state,
    save_delivery_state,
)


class ExistingCardDestination:
    def __init__(self) -> None:
        self.edited: list[int] = []

    async def edit_rich(
        self,
        *,
        chat_id: int,
        message_id: int,
        rich_html: str,
        fallback_html: str,
    ) -> int:
        self.edited.append(message_id)
        return message_id

    async def send_rich(
        self,
        *,
        chat_id: int,
        topic_id: int,
        rich_html: str,
        fallback_html: str,
        silent: bool,
    ) -> int:
        raise AssertionError("A calendar artifact failure must not create another card")

    async def send(self, *, chat_id: int, topic_id: int, text: str) -> int:
        raise AssertionError("A quiet corrective snapshot must not send a notification")


@pytest.mark.asyncio
async def test_calendar_file_failure_keeps_delivered_telegram_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 10, 29, 9, tzinfo=UTC)
    current = ScheduleEnvelope(
        group_key="3-СУЗСс-3",
        fetched_at=now,
        horizon_start=date(2026, 10, 26),
        horizon_end=date(2026, 11, 8),
        lessons=(
            Lesson(
                day=date(2026, 10, 30),
                starts_at=time(10, 45),
                ends_at=time(12, 15),
                subject="Геодезия",
            ),
        ),
        force_digest=True,
    )
    state_path = tmp_path / "state.json"
    initial = ScheduleDeliveryState(
        previous=replace(current, fetched_at=now - timedelta(minutes=15)),
        sent_reminders=("older-marker",),
        calendar_message_id=70,
    )
    save_delivery_state(state_path, initial)
    settings = Settings(
        telegram_bot_token=SecretStr("123456:token"),
        target_chat_id=-1001,
        schedule_topic_id=42,
        spbgasu_group_id=current.group_key,
        schedule_quiet_mode=True,
        _env_file=None,
    )
    destination = ExistingCardDestination()

    def fail_calendar_write(path: Path, content: str) -> None:
        raise OSError("calendar output unavailable")

    monkeypatch.setattr(publisher, "_write_calendar", fail_calendar_write)

    with pytest.raises(OSError, match="calendar output unavailable"):
        await publisher.publish_dispatched(
            settings,
            text_b64=encode_schedule_envelope(current),
            group_key=current.group_key,
            destination=destination,
            state_path=state_path,
            calendar_path=tmp_path / "calendar.ics",
            clock=lambda: now,
        )

    persisted = load_delivery_state(state_path)
    assert persisted.previous == current
    assert persisted.calendar_message_id == initial.calendar_message_id
    assert persisted.sent_reminders == initial.sent_reminders
    assert persisted.last_digest_date == now.date()
    assert destination.edited == [70]

    # A retry must not replay successfully completed Telegram work.
    repeated = await publisher.publish_dispatched(
        settings,
        text_b64=encode_schedule_envelope(current),
        group_key=current.group_key,
        destination=destination,
        state_path=state_path,
        clock=lambda: now,
    )
    assert repeated == ()
    assert destination.edited == [70]


def test_receiver_saves_state_before_optional_calendar_steps_even_after_failure() -> None:
    workflow = (
        Path(__file__).parents[1] / ".github/workflows/receive-schedule.yml"
    ).read_text(encoding="utf-8")
    send_job = workflow.split("\n  deploy-calendar:", 1)[0]
    steps = re.split(r"(?m)^      - name: ", send_job)[1:]
    publish_index = next(
        index for index, step in enumerate(steps) if "run: szs-hub publish-dispatched" in step
    )
    save_index = next(
        index for index, step in enumerate(steps) if "uses: actions/cache/save@" in step
    )

    assert save_index == publish_index + 1
    assert "if: always()" in steps[save_index]
    for index, step in enumerate(steps):
        if any(action in step for action in (
            "uses: actions/upload-artifact@",
            "uses: actions/configure-pages@",
            "uses: actions/upload-pages-artifact@",
        )):
            assert index > save_index


def test_reminder_cli_does_not_claim_a_notification_for_a_card_edit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = Settings(_env_file=None)

    async def edit_only(
        actual_settings: Settings, *, state_path: Path,
    ) -> tuple[int, ...]:
        assert actual_settings is settings
        return (70,)

    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(publisher, "publish_due_reminder", edit_only)

    assert cli.main(["publish-reminder"]) == 0
    output = capsys.readouterr()
    assert "Telegram обновлён" in output.out
    assert "70" in output.out
    assert "Напоминание отправлено" not in output.out
    assert output.err == ""
