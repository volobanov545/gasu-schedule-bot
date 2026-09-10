from __future__ import annotations

from datetime import UTC, datetime, time
from types import SimpleNamespace

import pytest
from aiogram.enums import ChatMemberStatus, ChatType
from pydantic import SecretStr

from szs_hub import app
from szs_hub.app import (
    RuntimeConfigurationError,
    _receives_telegram_updates,
    _runtime_configuration,
    _schedule_freshness_minutes,
    _telegram_startup_probe,
    _validate_runtime_settings,
    planned_recurring_jobs,
)
from szs_hub.config import FeatureProfile, Settings, TelegramMode


def _runtime_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "telegram_bot_token": SecretStr("123456:token"),
        "feature_profile": FeatureProfile.FULL,
        "telegram_mode": TelegramMode.POLLING,
        "target_chat_id": -1001,
        "schedule_topic_id": 10,
        "materials_topic_id": 11,
        "headman_user_id": 12,
        "spbgasu_group_id": "42",
        "live_processing_approved": True,
    }
    values.update(overrides)
    return Settings(**values)


def test_runtime_validation_requires_real_polling_configuration() -> None:
    _validate_runtime_settings(_runtime_settings())
    with pytest.raises(RuntimeConfigurationError, match="spbgasu_group_id"):
        _validate_runtime_settings(_runtime_settings(spbgasu_group_id=None))


def test_runtime_rejects_webhook_build() -> None:
    settings = _runtime_settings(
        telegram_mode=TelegramMode.WEBHOOK,
        telegram_webhook_url="https://example.test/hook",
        telegram_webhook_secret=SecretStr("secret"),
    )
    with pytest.raises(RuntimeConfigurationError, match="TELEGRAM_MODE=polling"):
        _validate_runtime_settings(settings)


def test_schedule_only_rejects_a_receive_capable_transport() -> None:
    settings = _runtime_settings(
        feature_profile=FeatureProfile.SCHEDULE_ONLY,
        telegram_mode=TelegramMode.POLLING,
        materials_topic_id=None,
        headman_user_id=None,
    )

    with pytest.raises(RuntimeConfigurationError, match="TELEGRAM_MODE=outbound_only"):
        _validate_runtime_settings(settings)


def test_live_processing_is_blocked_even_with_ai_disabled() -> None:
    settings = _runtime_settings(live_processing_approved=False, ai_kill_switch=True)
    with pytest.raises(RuntimeConfigurationError, match="LIVE_PROCESSING_APPROVED"):
        _validate_runtime_settings(settings)


@pytest.mark.parametrize("environment", ["local", "staging", "production"])
async def test_unapproved_run_stops_before_database_or_network_clients(
    monkeypatch: pytest.MonkeyPatch, environment: str
) -> None:
    def unexpected_side_effect(*args: object, **kwargs: object) -> None:
        raise AssertionError("unapproved run must not construct a database or network client")

    monkeypatch.setattr(app, "create_database_engine", unexpected_side_effect)
    monkeypatch.setattr(app, "Bot", unexpected_side_effect)
    monkeypatch.setattr(app, "SpbGasuClient", unexpected_side_effect)
    monkeypatch.setattr(app, "OpenAICompatibleProvider", unexpected_side_effect)
    settings = _runtime_settings(
        app_env=environment,
        alert_user_id=13,
        backup_status_path="/var/lib/szs-hub-backup/last-success",
        live_processing_approved=False,
    )
    with pytest.raises(RuntimeConfigurationError, match="LIVE_PROCESSING_APPROVED"):
        await app.run(settings)


def test_recurring_jobs_are_bucketed_and_evening_is_once_per_local_day() -> None:
    settings = _runtime_settings(
        schedule_sync_minutes=15,
        evening_schedule_time=time(20, 30),
    )
    before = planned_recurring_jobs(settings, datetime(2026, 8, 30, 17, 29, tzinfo=UTC))
    after = planned_recurring_jobs(settings, datetime(2026, 8, 30, 17, 30, tzinfo=UTC))

    assert {job.kind for job in before} == {
        "schedule.sync",
        "maintenance.minute",
        "maintenance.retention",
    }
    assert {job.kind for job in after} == {
        "schedule.sync",
        "maintenance.minute",
        "maintenance.retention",
        "schedule.evening",
    }
    retention = next(job for job in before if job.kind == "maintenance.retention")
    assert retention.idempotency_key == "maintenance-retention:2026-08-30"
    evening = next(job for job in after if job.kind == "schedule.evening")
    assert evening.idempotency_key == "schedule-evening:2026-08-30"


def test_schedule_freshness_is_shorter_than_health_staleness() -> None:
    settings = _runtime_settings(schedule_sync_minutes=15, schedule_stale_after_minutes=360)
    assert _schedule_freshness_minutes(settings) == 30


def test_recovery_configuration_omits_every_secret() -> None:
    settings = _runtime_settings(
        ai_api_key=SecretStr("ai-secret"),
        telegram_webhook_secret=SecretStr("webhook-secret"),
        spbgasu_session=SecretStr("session-secret"),
    )
    manifest = _runtime_configuration(settings)
    rendered = repr(manifest)

    assert "telegram_bot_token" not in manifest
    assert "ai_api_key" not in manifest
    assert "telegram_webhook_secret" not in manifest
    assert "spbgasu_session" not in manifest
    assert "live_processing_approved" not in manifest
    assert "secret" not in rendered
    assert manifest["version"] == 5
    assert manifest["ai_per_user_daily_request_limit"] == 10
    assert manifest["ai_monthly_request_limit"] == 1_000
    assert manifest["material_extraction_enabled"] is False
    assert manifest["material_ocr_enabled"] is False
    assert manifest["material_auto_delete_source"] is False


def test_schedule_only_runtime_needs_no_member_or_material_identifiers() -> None:
    settings = _runtime_settings(
        feature_profile=FeatureProfile.SCHEDULE_ONLY,
        telegram_mode=TelegramMode.OUTBOUND_ONLY,
        materials_topic_id=None,
        headman_user_id=None,
        alert_user_id=None,
    )

    _validate_runtime_settings(settings)
    manifest = _runtime_configuration(settings)

    assert manifest["feature_profile"] == "schedule_only"
    assert "headman_user_id" not in manifest
    assert "admin_user_ids" not in manifest
    assert "materials_topic_id" not in manifest
    assert "ai_model" not in manifest
    assert _receives_telegram_updates(settings) is False
    assert _receives_telegram_updates(_runtime_settings()) is True


def test_schedule_only_recurring_jobs_publish_schedules_only() -> None:
    settings = _runtime_settings(
        feature_profile=FeatureProfile.SCHEDULE_ONLY,
        telegram_mode=TelegramMode.OUTBOUND_ONLY,
        evening_schedule_time=time(20, 30),
    )

    planned = planned_recurring_jobs(settings, datetime(2026, 8, 30, 17, 30, tzinfo=UTC))

    assert {job.kind for job in planned} == {
        "schedule.sync",
        "schedule.evening",
        "runtime.heartbeat",
    }


@pytest.mark.asyncio
async def test_schedule_only_probe_needs_no_admin_or_person_membership_lookup() -> None:
    class MemberBot:
        def __init__(self) -> None:
            self.membership_lookups: list[tuple[int, int]] = []

        async def get_me(self) -> SimpleNamespace:
            return SimpleNamespace(id=99)

        async def get_chat(self, _chat_id: int) -> SimpleNamespace:
            return SimpleNamespace(type=ChatType.SUPERGROUP, is_forum=True)

        async def get_chat_member(self, chat_id: int, user_id: int) -> SimpleNamespace:
            self.membership_lookups.append((chat_id, user_id))
            return SimpleNamespace(status=ChatMemberStatus.MEMBER)

    bot = MemberBot()
    settings = _runtime_settings(
        feature_profile=FeatureProfile.SCHEDULE_ONLY,
        telegram_mode=TelegramMode.OUTBOUND_ONLY,
        materials_topic_id=None,
        headman_user_id=None,
    )

    await _telegram_startup_probe(bot, settings)  # type: ignore[arg-type]

    assert bot.membership_lookups == [(-1001, 99)]
