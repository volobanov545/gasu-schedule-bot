from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from szs_hub import alerting
from szs_hub.alerting import (
    AlertCode,
    AlertDeliveryError,
    AlertOutcome,
    AlertSettings,
    AlertStateError,
    render_owner_alert,
    send_owner_alert,
)


class FakeSession:
    def __init__(self, *, close_error: Exception | None = None) -> None:
        self.closed = False
        self.close_error = close_error

    async def close(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class FakeBot:
    instances: list[FakeBot] = []
    send_error: Exception | None = None
    close_error: Exception | None = None
    pause_during_send = False

    def __init__(self, token: str) -> None:
        self.credential = token
        self.session = FakeSession(close_error=self.close_error)
        self.calls: list[dict[str, Any]] = []
        self.instances.append(self)

    async def send_message(self, **kwargs: Any) -> object:
        self.calls.append(kwargs)
        if self.pause_during_send:
            await asyncio.sleep(60)
        if self.send_error is not None:
            raise self.send_error
        return object()


@pytest.fixture(autouse=True)
def reset_fake_bot(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeBot.instances = []
    FakeBot.send_error = None
    FakeBot.close_error = None
    FakeBot.pause_during_send = False
    monkeypatch.setattr(alerting, "Bot", FakeBot)


def _settings() -> AlertSettings:
    return AlertSettings(
        telegram_bot_token=SecretStr("123456:alert-token"),
        alert_user_id=987_654,
        _env_file=None,
    )


def test_alert_settings_ignore_broken_unrelated_runtime_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "not-a-real-environment")
    monkeypatch.setenv("DATABASE_URL", "not-a-database-url")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:alert-token")
    monkeypatch.setenv("ALERT_USER_ID", "123")

    settings = AlertSettings(_env_file=None)

    assert settings.alert_user_id == 123


def test_alert_settings_require_private_recipient_and_token() -> None:
    with pytest.raises(ValidationError):
        AlertSettings(_env_file=None)
    with pytest.raises(ValidationError):
        AlertSettings(
            telegram_bot_token=SecretStr("123456:alert-token"),
            alert_user_id=0,
            _env_file=None,
        )


def test_alert_code_and_rendering_are_fixed_plain_text() -> None:
    with pytest.raises(ValueError):
        AlertCode("../../arbitrary-unit")

    rendered = render_owner_alert(
        AlertCode.BACKUP,
        now=datetime(2026, 9, 1, 3, 15, tzinfo=UTC),
    )

    assert rendered == (
        "⚠️ SZS Hub: критический сбой.\n"
        "Компонент: резервное копирование.\n"
        "Время UTC: 2026-09-01 03:15:00.\n"
        "Проверьте состояние сервиса и ограниченный журнал на сервере."
    )
    assert "<" not in rendered
    assert ">" not in rendered


async def test_successful_alert_is_private_plain_text_and_atomically_cooled_down(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 9, 1, 3, 15, tzinfo=UTC)

    first = await send_owner_alert(
        _settings(),
        AlertCode.HEALTH,
        state_directory=tmp_path,
        now=now,
    )
    second = await send_owner_alert(
        _settings(),
        AlertCode.HEALTH,
        state_directory=tmp_path,
        now=now + timedelta(hours=1),
    )

    assert first is AlertOutcome.SENT
    assert second is AlertOutcome.SUPPRESSED
    assert len(FakeBot.instances) == 1
    bot = FakeBot.instances[0]
    assert bot.credential == "123456:alert-token"
    assert bot.session.closed is True
    assert bot.calls == [
        {
            "chat_id": 987_654,
            "text": render_owner_alert(AlertCode.HEALTH, now=now),
            "parse_mode": None,
            "disable_notification": False,
            "request_timeout": 20,
        }
    ]
    assert (tmp_path / "health.last-success").read_text(encoding="utf-8") == (
        "2026-09-01T03:15:00Z\n"
    )
    temporary_files = await asyncio.to_thread(lambda: list(tmp_path.glob("*.tmp")))
    assert temporary_files == []


async def test_expired_cooldown_sends_a_reminder(tmp_path: Path) -> None:
    now = datetime(2026, 9, 1, tzinfo=UTC)
    await send_owner_alert(
        _settings(), AlertCode.MAIN, state_directory=tmp_path, now=now
    )

    outcome = await send_owner_alert(
        _settings(),
        AlertCode.MAIN,
        state_directory=tmp_path,
        now=now + timedelta(minutes=31),
    )

    assert outcome is AlertOutcome.SENT
    assert len(FakeBot.instances) == 2


@pytest.mark.parametrize(
    "marker_text",
    [
        "not-a-timestamp",
        "x" * 129,
        "2027-09-01T00:00:00Z",
    ],
)
async def test_invalid_oversize_or_future_marker_never_suppresses(
    tmp_path: Path,
    marker_text: str,
) -> None:
    (tmp_path / "backup.last-success").write_text(marker_text, encoding="utf-8")

    outcome = await send_owner_alert(
        _settings(),
        AlertCode.BACKUP,
        state_directory=tmp_path,
        now=datetime(2026, 9, 1, tzinfo=UTC),
    )

    assert outcome is AlertOutcome.SENT
    assert len(FakeBot.instances) == 1


async def test_delivery_failure_does_not_start_cooldown(tmp_path: Path) -> None:
    FakeBot.send_error = RuntimeError("private provider details must not escape")

    with pytest.raises(AlertDeliveryError) as captured:
        await send_owner_alert(
            _settings(),
            AlertCode.MIGRATE,
            state_directory=tmp_path,
            now=datetime(2026, 9, 1, tzinfo=UTC),
        )

    assert captured.value.category == "RuntimeError"
    assert "private provider details" not in str(captured.value)
    assert FakeBot.instances[0].session.closed is True
    assert not (tmp_path / "migrate.last-success").exists()


async def test_timeout_closes_session_and_does_not_start_cooldown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeBot.pause_during_send = True
    monkeypatch.setattr(alerting, "_TELEGRAM_TIMEOUT_SECONDS", 0.001)

    with pytest.raises(AlertDeliveryError) as captured:
        await send_owner_alert(
            _settings(),
            AlertCode.MAIN,
            state_directory=tmp_path,
            now=datetime(2026, 9, 1, tzinfo=UTC),
        )

    assert captured.value.category == "TimeoutError"
    assert FakeBot.instances[0].session.closed is True
    assert not (tmp_path / "main.last-success").exists()


async def test_session_close_failure_is_a_delivery_failure(tmp_path: Path) -> None:
    FakeBot.close_error = RuntimeError("close details")

    with pytest.raises(AlertDeliveryError) as captured:
        await send_owner_alert(
            _settings(),
            AlertCode.MAIN,
            state_directory=tmp_path,
            now=datetime(2026, 9, 1, tzinfo=UTC),
        )

    assert captured.value.category == "RuntimeError"
    assert not (tmp_path / "main.last-success").exists()


async def test_marker_write_failure_is_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_write(path: Path, value: datetime) -> None:
        raise PermissionError("private filesystem details")

    monkeypatch.setattr(alerting, "_write_success_marker", fail_write)

    with pytest.raises(AlertStateError) as captured:
        await send_owner_alert(
            _settings(),
            AlertCode.HEALTH,
            state_directory=tmp_path,
            now=datetime(2026, 9, 1, tzinfo=UTC),
        )

    assert captured.value.category == "PermissionError"
    assert "private filesystem details" not in str(captured.value)
    assert FakeBot.instances[0].session.closed is True


async def test_naive_timestamp_is_rejected_before_network(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        await send_owner_alert(
            _settings(),
            AlertCode.HEALTH,
            state_directory=tmp_path,
            now=datetime(2026, 9, 1),
        )

    assert FakeBot.instances == []
