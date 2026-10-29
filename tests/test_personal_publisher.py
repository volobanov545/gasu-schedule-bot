from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from szs_hub.config import ScheduleSender, Settings
from szs_hub.domain.schedule import Lesson
from szs_hub.publisher import publish_dispatched, publish_due_reminder
from szs_hub.schedule.ci import (
    ScheduleDeliveryState,
    ScheduleEnvelope,
    encode_schedule_envelope,
    load_delivery_state,
    save_delivery_state,
)

_NOW = datetime(2026, 10, 29, 9, tzinfo=UTC)
_USER_KEY = "user:123:-1001:42"
_REMINDER = "first:2026-10-30:10:45:00"


class RecordingDestination:
    def __init__(self, *, message_id: int = 20000) -> None:
        self.message_id = message_id
        self.created: list[tuple[int, int, str, bool]] = []
        self.edited: list[tuple[int, int, str]] = []
        self.notifications: list[tuple[int, int, str]] = []
        self.pinned: list[tuple[int, int]] = []

    async def send(self, *, chat_id: int, topic_id: int, text: str) -> int:
        self.notifications.append((chat_id, topic_id, text))
        return self.message_id + 1

    async def send_rich(
        self, *, chat_id: int, topic_id: int, rich_html: str,
        fallback_html: str, silent: bool,
    ) -> int:
        self.created.append((chat_id, topic_id, rich_html, silent))
        return self.message_id

    async def edit_rich(
        self, *, chat_id: int, message_id: int, rich_html: str, fallback_html: str,
    ) -> int:
        self.edited.append((chat_id, message_id, rich_html))
        return message_id

    async def pin_message(self, *, chat_id: int, message_id: int) -> None:
        self.pinned.append((chat_id, message_id))


class PersonalDestination(RecordingDestination):
    def __init__(self, *, key: str = _USER_KEY, message_id: int = 20000) -> None:
        super().__init__(message_id=message_id)
        self.card_key = key


def _settings(*, sender: str = "user", **overrides: object) -> Settings:
    values: dict[str, object] = {
        "schedule_sender": sender,
        "telegram_user_credentials": SecretStr("private-test-session-do-not-serialize"),
        "target_chat_id": -1001,
        "schedule_topic_id": 42,
        "spbgasu_group_id": "3-СУЗСс-3",
        "schedule_quiet_mode": True,
        "_env_file": None,
    }
    values.update(overrides)
    return Settings(**values)


def _envelope(*, fetched_at: datetime = _NOW) -> ScheduleEnvelope:
    return ScheduleEnvelope(
        group_key="3-СУЗСс-3",
        fetched_at=fetched_at,
        horizon_start=date(2026, 10, 26),
        horizon_end=date(2026, 11, 8),
        lessons=(Lesson(
            day=date(2026, 10, 30), starts_at=time(10, 45), ends_at=time(12, 15),
            subject="Геодезия", room="407", teacher="Иванов Иван Иванович",
        ),),
    )


async def _publish(
    state_path: Path, destination: RecordingDestination, *,
    envelope: ScheduleEnvelope | None = None, settings: Settings | None = None,
) -> tuple[int, ...]:
    current = envelope or _envelope()
    return await publish_dispatched(
        settings or _settings(),
        text_b64=encode_schedule_envelope(current),
        group_key=current.group_key,
        destination=destination,
        state_path=state_path,
        clock=lambda: current.fetched_at,
    )


@pytest.mark.asyncio
async def test_personal_migration_preserves_bot_card_and_edits_its_own_card_next_time(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.json"
    previous = _envelope(fetched_at=_NOW - timedelta(minutes=15))
    initial = ScheduleDeliveryState(
        previous=previous, calendar_message_id=15396,
        sent_reminders=(_REMINDER,), last_digest_date=_NOW.date(),
    )
    save_delivery_state(path, initial)
    destination = PersonalDestination()

    assert await _publish(path, destination) == (20000,)

    state = load_delivery_state(path)
    assert state.calendar_message_id == 15396
    assert dict(state.user_calendar_cards) == {_USER_KEY: 20000}
    assert state.sent_reminders == initial.sent_reminders
    assert state.last_digest_date == initial.last_digest_date
    assert state.previous == _envelope()
    assert destination.edited == []
    assert len(destination.created) == 1
    assert destination.created[0][0:2] == (-1001, 42)
    assert destination.created[0][3] is True
    assert destination.pinned == [(-1001, 20000)]
    assert destination.notifications == []

    newer = _envelope(fetched_at=_NOW + timedelta(minutes=15))
    assert await _publish(path, destination, envelope=newer) == (20000,)
    assert len(destination.created) == 1
    assert [row[1] for row in destination.edited] == [20000]
    assert load_delivery_state(path).calendar_message_id == 15396


@pytest.mark.asyncio
async def test_rollback_to_bot_uses_preserved_bot_card_without_destroying_user_mapping(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.json"
    initial = ScheduleDeliveryState(
        previous=_envelope(fetched_at=_NOW - timedelta(minutes=15)),
        calendar_message_id=15396,
        user_calendar_cards=((_USER_KEY, 20000),),
        sent_reminders=(_REMINDER,),
    )
    save_delivery_state(path, initial)
    bot = RecordingDestination()

    assert await _publish(path, bot, settings=_settings(sender="bot")) == (15396,)

    assert [row[1] for row in bot.edited] == [15396]
    assert bot.created == []
    state = load_delivery_state(path)
    assert state.calendar_message_id == 15396
    assert state.user_calendar_cards == initial.user_calendar_cards
    assert state.sent_reminders == initial.sent_reminders


@pytest.mark.asyncio
@pytest.mark.parametrize("key,chat_id,topic_id", [
    ("user:456:-1001:42", -1001, 42),
    ("user:123:-1002:42", -1002, 42),
    ("user:123:-1001:43", -1001, 43),
])
async def test_personal_card_is_separate_for_each_account_chat_and_topic(
    tmp_path: Path, key: str, chat_id: int, topic_id: int,
) -> None:
    path = tmp_path / "state.json"
    save_delivery_state(path, ScheduleDeliveryState(
        previous=_envelope(fetched_at=_NOW - timedelta(minutes=15)),
        calendar_message_id=15396, user_calendar_cards=((_USER_KEY, 20000),),
    ))
    destination = PersonalDestination(key=key, message_id=21000)

    await _publish(path, destination, settings=_settings(
        target_chat_id=chat_id, schedule_topic_id=topic_id,
    ))

    assert destination.edited == []
    assert len(destination.created) == 1
    assert destination.created[0][0:2] == (chat_id, topic_id)
    assert dict(load_delivery_state(path).user_calendar_cards) == {
        _USER_KEY: 20000, key: 21000,
    }


@pytest.mark.asyncio
async def test_personal_backup_tick_preserves_deduplication_and_never_edits_bot_card(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.json"
    initial = ScheduleDeliveryState(
        previous=_envelope(), calendar_message_id=15396,
        user_calendar_cards=((_USER_KEY, 20000),), sent_reminders=(_REMINDER,),
    )
    save_delivery_state(path, initial)
    destination = PersonalDestination()

    result = await publish_due_reminder(
        _settings(schedule_quiet_mode=False), destination=destination, state_path=path,
        clock=lambda: datetime(2026, 10, 30, 5, 45, tzinfo=UTC),
    )

    assert result == (20000,)
    assert [row[1] for row in destination.edited] == [20000]
    assert destination.notifications == []
    assert destination.created == []
    assert load_delivery_state(path) == initial


@pytest.mark.asyncio
async def test_user_delivery_failure_never_falls_back_or_overwrites_bot_state(
    tmp_path: Path,
) -> None:
    class FailedPersonalDestination(PersonalDestination):
        async def edit_rich(self, **kwargs: object) -> int:
            raise RuntimeError("personal account temporarily unavailable")

    path = tmp_path / "state.json"
    initial = ScheduleDeliveryState(
        previous=_envelope(fetched_at=_NOW - timedelta(minutes=15)),
        calendar_message_id=15396, user_calendar_cards=((_USER_KEY, 20000),),
    )
    save_delivery_state(path, initial)
    destination = FailedPersonalDestination()

    with pytest.raises(RuntimeError, match="temporarily unavailable"):
        await _publish(path, destination)

    assert destination.created == []
    assert destination.notifications == []
    assert load_delivery_state(path) == initial


@pytest.mark.asyncio
async def test_personal_credentials_never_enter_persisted_schedule_state(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    settings = _settings()

    await _publish(path, PersonalDestination(), settings=settings)

    content = path.read_text(encoding="utf-8")
    assert settings.telegram_user_credentials is not None
    assert settings.telegram_user_credentials.get_secret_value() not in content
    assert "telegram_user_credentials" not in content
    assert "session" not in json.loads(content)
    assert dict(load_delivery_state(path).user_calendar_cards) == {_USER_KEY: 20000}


def test_default_sender_remains_bot_even_when_personal_credentials_are_present() -> None:
    settings = Settings(
        telegram_user_credentials=SecretStr("unused-personal-session"), _env_file=None,
    )

    assert settings.schedule_sender is ScheduleSender.BOT


@pytest.mark.parametrize("credentials", [None, SecretStr("")])
def test_user_mode_requires_nonempty_credentials(credentials: SecretStr | None) -> None:
    with pytest.raises(ValidationError, match="TELEGRAM_USER_CREDENTIALS"):
        _settings(telegram_user_credentials=credentials)


@pytest.mark.parametrize("overrides", [
    {"telegram_mode": "polling"},
    {"feature_profile": "full"},
])
def test_user_mode_rejects_non_outbound_or_full_runtime(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="schedule_only/outbound_only"):
        _settings(**overrides)


def test_production_user_mode_does_not_require_a_bot_token() -> None:
    settings = _settings(app_env="production", telegram_bot_token=None)

    assert settings.schedule_sender is ScheduleSender.USER
    assert settings.telegram_bot_token is None


def test_production_bot_mode_still_requires_bot_token() -> None:
    with pytest.raises(ValidationError, match="telegram_bot_token"):
        _settings(sender="bot", app_env="production", telegram_bot_token=None)


def test_legacy_schema3_load_preserves_source_and_bot_card(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    original = ScheduleDeliveryState(
        previous=_envelope(), calendar_message_id=15396,
        last_digest_date=_NOW.date(), sent_reminders=(_REMINDER,),
    )
    save_delivery_state(path, original)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["schema"] = 3
    payload.pop("user_calendar_cards")
    # Construct a legacy input fixture; production writes are exercised above.
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert load_delivery_state(path) == replace(original, user_calendar_cards=())
