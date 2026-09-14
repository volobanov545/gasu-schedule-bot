"""Typed application configuration with fail-closed production validation."""

from __future__ import annotations

from datetime import time
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppEnvironment(StrEnum):
    LOCAL = "local"
    STAGING = "staging"
    PRODUCTION = "production"


class TelegramMode(StrEnum):
    OUTBOUND_ONLY = "outbound_only"
    POLLING = "polling"
    WEBHOOK = "webhook"


class FeatureProfile(StrEnum):
    """Explicitly selected data-processing surface."""

    SCHEDULE_ONLY = "schedule_only"
    FULL = "full"


class Settings(BaseSettings):
    """Configuration loaded from environment variables or a local ignored `.env`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: AppEnvironment = AppEnvironment.LOCAL
    feature_profile: FeatureProfile = FeatureProfile.SCHEDULE_ONLY
    # Operator release acknowledgement, not member consent or legal certification.
    live_processing_approved: bool = False
    log_level: str = "INFO"
    timezone: str = "Europe/Moscow"
    database_url: str = "sqlite+aiosqlite:///./data/szs_hub.sqlite3"
    sqlite_wal_backport_confirmed: bool = False
    schedule_stale_after_minutes: int = Field(default=360, ge=15, le=10_080)
    schedule_sync_minutes: int = Field(default=15, ge=5, le=240)
    schedule_calendar_url: str | None = None
    runtime_heartbeat_stale_minutes: int = Field(default=5, ge=2, le=60)
    telegram_probe_interval_minutes: int = Field(default=15, ge=5, le=240)
    telegram_probe_stale_minutes: int = Field(default=30, ge=10, le=480)
    backup_status_path: Path | None = None
    backup_stale_after_hours: int = Field(default=36, ge=12, le=336)

    telegram_mode: TelegramMode = TelegramMode.OUTBOUND_ONLY
    telegram_bot_token: SecretStr | None = None
    telegram_webhook_url: str | None = None
    telegram_webhook_secret: SecretStr | None = None
    target_chat_id: int | None = None
    general_topic_id: int | None = None
    schedule_topic_id: int | None = None
    materials_topic_id: int | None = None
    assistant_topic_id: int | None = None
    headman_user_id: int | None = None
    alert_user_id: int | None = None
    admin_user_ids: tuple[int, ...] = ()
    attendance_reaction: str = "❤"
    attendance_window_minutes: int = Field(default=25, ge=5, le=120)
    evening_schedule_time: time = time(hour=20, minute=30)

    spbgasu_base_url: str = "https://rasp.spbgasu.ru"
    spbgasu_group_id: str | None = None
    spbgasu_group_name: str | None = None
    spbgasu_session: SecretStr | None = None

    ai_provider: str = "openai_compatible"
    ai_base_url: str | None = None
    ai_api_key: SecretStr | None = None
    ai_model: str | None = None
    ai_timeout_seconds: float = Field(default=30.0, ge=1.0, le=180.0)
    ai_max_retries: int = Field(default=2, ge=0, le=6)
    ai_max_response_bytes: int = Field(default=1_000_000, ge=16_384, le=10_000_000)
    ai_kill_switch: bool = False
    ai_daily_request_limit: int = Field(default=100, ge=1, le=10_000)
    ai_daily_token_limit: int = Field(default=100_000, ge=1_000, le=100_000_000)
    ai_per_user_daily_request_limit: int = Field(default=10, ge=1, le=1_000)
    ai_monthly_request_limit: int = Field(default=1_000, ge=1, le=100_000)
    ai_monthly_token_limit: int = Field(default=1_000_000, ge=1_000, le=1_000_000_000)
    ai_token_reservation_per_request: int = Field(default=3_000, ge=256, le=100_000)

    material_auto_copy_threshold: float = Field(default=0.95, ge=0.0, le=1.0)
    material_admin_review_threshold: float = Field(default=0.70, ge=0.0, le=1.0)
    material_extraction_enabled: bool = False
    material_auto_delete_source: bool = False

    @field_validator("admin_user_ids", mode="before")
    @classmethod
    def parse_admin_ids(cls, value: object) -> object:
        if value in (None, ""):
            return ()
        if isinstance(value, str):
            return tuple(int(item.strip()) for item in value.split(",") if item.strip())
        return value

    @field_validator("attendance_reaction")
    @classmethod
    def validate_reaction(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("attendance reaction cannot be empty")
        if len(cleaned) > 16:
            raise ValueError("attendance reaction is unexpectedly long")
        return cleaned

    @model_validator(mode="after")
    def validate_thresholds_and_deployment(self) -> Settings:
        if self.material_admin_review_threshold > self.material_auto_copy_threshold:
            raise ValueError("review threshold must not exceed auto-copy threshold")
        if self.telegram_probe_stale_minutes <= self.telegram_probe_interval_minutes:
            raise ValueError("Telegram probe staleness must exceed its interval")

        if self.telegram_mode is TelegramMode.WEBHOOK:
            if not self.telegram_webhook_url or not self.telegram_webhook_secret:
                raise ValueError("webhook mode requires URL and secret")
            if not self.telegram_webhook_url.startswith("https://"):
                raise ValueError("Telegram webhook URL must use HTTPS")

        if self.app_env is not AppEnvironment.LOCAL:
            _require_https("spbgasu_base_url", self.spbgasu_base_url)
            if self.schedule_calendar_url is not None:
                _require_https("schedule_calendar_url", self.schedule_calendar_url)
            if self.ai_base_url is not None:
                _require_https("ai_base_url", self.ai_base_url)
            if self.ai_enabled and self.ai_max_retries:
                raise ValueError(
                    "AI retries must be disabled outside local development unless "
                    "provider idempotency is implemented"
                )
            required: dict[str, object] = {
                "telegram_bot_token": self.telegram_bot_token,
                "target_chat_id": self.target_chat_id,
                "schedule_topic_id": self.schedule_topic_id,
                "spbgasu_group_id": self.spbgasu_group_id,
            }
            if self.feature_profile is FeatureProfile.FULL:
                required.update(
                    {
                        "materials_topic_id": self.materials_topic_id,
                        "headman_user_id": self.headman_user_id,
                        "alert_user_id": self.alert_user_id,
                        "backup_status_path": self.backup_status_path,
                    }
                )
            missing = sorted(name for name, value in required.items() if value is None)
            if missing:
                raise ValueError(f"deployment configuration is incomplete: {', '.join(missing)}")

        if self.target_chat_id is not None and self.target_chat_id >= 0:
            raise ValueError("target_chat_id must be a negative Telegram group identifier")
        if self.headman_user_id is not None and self.headman_user_id <= 0:
            raise ValueError("headman_user_id must be a positive Telegram user identifier")
        if self.alert_user_id is not None and self.alert_user_id <= 0:
            raise ValueError("alert_user_id must be a positive Telegram user identifier")
        if any(user_id <= 0 for user_id in self.admin_user_ids):
            raise ValueError("admin_user_ids must contain positive Telegram user identifiers")
        if len(set(self.admin_user_ids)) != len(self.admin_user_ids):
            raise ValueError("admin_user_ids must not contain duplicates")
        topics = {
            name: value
            for name, value in {
                "general_topic_id": self.general_topic_id,
                "schedule_topic_id": self.schedule_topic_id,
                "materials_topic_id": self.materials_topic_id,
                "assistant_topic_id": self.assistant_topic_id,
            }.items()
            if value is not None
        }
        if any(value <= 0 for value in topics.values()):
            raise ValueError("topic identifiers must be positive")
        if len(set(topics.values())) != len(topics):
            raise ValueError("configured topic identifiers must be distinct")
        if self.spbgasu_group_id is not None and not self.spbgasu_group_id.strip():
            raise ValueError("spbgasu_group_id cannot be blank")

        return self

    @property
    def ai_enabled(self) -> bool:
        return bool(
            not self.ai_kill_switch
            and self.ai_base_url
            and self.ai_api_key
            and self.ai_model
        )


def _require_https(name: str, value: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        raise ValueError(f"{name} must use an absolute HTTPS URL outside local development")
