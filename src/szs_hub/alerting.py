"""Private owner alerts for critical systemd failures."""

from __future__ import annotations

import asyncio
import os
import secrets
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from aiogram import Bot
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_ALERT_STATE_DIRECTORY = Path("/var/lib/szs-hub-alert")
_MAX_MARKER_BYTES = 128
_TELEGRAM_TIMEOUT_SECONDS = 20


class AlertCode(StrEnum):
    """The only failure classes accepted from systemd template instances."""

    MAIN = "main"
    HEALTH = "health"
    BACKUP = "backup"
    MIGRATE = "migrate"


class AlertOutcome(StrEnum):
    """Safe, user-facing outcomes for a single alert attempt."""

    SENT = "sent"
    SUPPRESSED = "suppressed"


class AlertSettings(BaseSettings):
    """Minimal settings that remain usable when unrelated runtime config is broken."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        validate_default=True,
    )

    telegram_bot_token: SecretStr = Field(default=SecretStr(""), min_length=1)
    alert_user_id: int = Field(default=0, gt=0)


class AlertDeliveryError(RuntimeError):
    """Telegram alert delivery failed without retaining provider error text."""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__("owner alert delivery failed")


class AlertStateError(RuntimeError):
    """The successful-delivery cooldown could not be persisted."""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__("owner alert state could not be persisted")


_ALERT_LABELS = {
    AlertCode.MAIN: "основной сервис",
    AlertCode.HEALTH: "проверка состояния",
    AlertCode.BACKUP: "резервное копирование",
    AlertCode.MIGRATE: "миграция базы данных",
}

_SUCCESS_COOLDOWNS = {
    AlertCode.MAIN: timedelta(minutes=30),
    AlertCode.HEALTH: timedelta(hours=6),
    AlertCode.BACKUP: timedelta(hours=6),
    AlertCode.MIGRATE: timedelta(minutes=30),
}


def render_owner_alert(code: AlertCode, *, now: datetime | None = None) -> str:
    """Render a fixed plain-text alert without logs, identifiers, or exception text."""

    sent_at = _aware_utc(now)
    return (
        "⚠️ SZS Hub: критический сбой.\n"
        f"Компонент: {_ALERT_LABELS[code]}.\n"
        f"Время UTC: {sent_at.strftime('%Y-%m-%d %H:%M:%S')}.\n"
        "Проверьте состояние сервиса и ограниченный журнал на сервере."
    )


async def send_owner_alert(
    settings: AlertSettings,
    code: AlertCode,
    *,
    state_directory: Path = DEFAULT_ALERT_STATE_DIRECTORY,
    now: datetime | None = None,
) -> AlertOutcome:
    """Send one private alert unless the same class succeeded inside its cooldown."""

    sent_at = _aware_utc(now)
    marker = state_directory / f"{code.value}.last-success"
    last_success = _read_success_marker(marker)
    if last_success is not None:
        elapsed = sent_at - last_success
        if timedelta(0) <= elapsed < _SUCCESS_COOLDOWNS[code]:
            return AlertOutcome.SUPPRESSED

    await _deliver_telegram(settings, render_owner_alert(code, now=sent_at))
    try:
        _write_success_marker(marker, sent_at)
    except OSError as exc:
        raise AlertStateError(type(exc).__name__) from None
    return AlertOutcome.SENT


async def _deliver_telegram(settings: AlertSettings, text: str) -> None:
    bot: Bot | None = None
    error_category: str | None = None
    try:
        bot = Bot(token=settings.telegram_bot_token.get_secret_value())
        async with asyncio.timeout(_TELEGRAM_TIMEOUT_SECONDS):
            await bot.send_message(
                chat_id=settings.alert_user_id,
                text=text,
                parse_mode=None,
                disable_notification=False,
                request_timeout=_TELEGRAM_TIMEOUT_SECONDS,
            )
    except Exception as exc:
        error_category = type(exc).__name__
    finally:
        if bot is not None:
            try:
                await bot.session.close()
            except Exception as exc:
                if error_category is None:
                    error_category = type(exc).__name__
    if error_category is not None:
        raise AlertDeliveryError(error_category)


def _read_success_marker(path: Path) -> datetime | None:
    """Read one bounded regular marker; malformed state fails open to a new send."""

    try:
        metadata = path.lstat()
        if path.is_symlink() or not path.is_file() or metadata.st_size > _MAX_MARKER_BYTES:
            return None
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _write_success_marker(path: Path, value: datetime) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    descriptor: int | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            descriptor = None
            stream.write(value.astimezone(UTC).isoformat().replace("+00:00", "Z"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _aware_utc(value: datetime | None) -> datetime:
    candidate = value or datetime.now(UTC)
    if candidate.tzinfo is None or candidate.utcoffset() is None:
        raise ValueError("alert timestamp must be timezone-aware")
    return candidate.astimezone(UTC)
