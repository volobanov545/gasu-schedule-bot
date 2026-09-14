from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from szs_hub.config import AppEnvironment, FeatureProfile, Settings, TelegramMode


def test_local_settings_are_safe_without_secrets() -> None:
    settings = Settings(_env_file=None)

    assert settings.app_env is AppEnvironment.LOCAL
    assert settings.feature_profile is FeatureProfile.SCHEDULE_ONLY
    assert settings.telegram_mode is TelegramMode.OUTBOUND_ONLY
    assert settings.ai_enabled is False
    assert settings.live_processing_approved is False
    assert settings.material_auto_delete_source is False


def test_non_local_environment_requires_telegram_configuration() -> None:
    with pytest.raises(ValidationError, match="deployment configuration is incomplete"):
        Settings(app_env="staging", _env_file=None)


def test_webhook_requires_https_secret() -> None:
    with pytest.raises(ValidationError, match="requires URL and secret"):
        Settings(telegram_mode="webhook", _env_file=None)


def test_review_threshold_cannot_exceed_auto_copy_threshold() -> None:
    with pytest.raises(ValidationError, match="review threshold"):
        Settings(
            material_admin_review_threshold=0.96,
            material_auto_copy_threshold=0.95,
            _env_file=None,
        )


def test_admin_ids_are_parsed_from_csv() -> None:
    settings = Settings(admin_user_ids="1, 2,3", _env_file=None)

    assert settings.admin_user_ids == (1, 2, 3)


def test_non_local_environment_requires_dedicated_alert_recipient() -> None:
    with pytest.raises(ValidationError, match="alert_user_id"):
        Settings(
            app_env="staging",
            feature_profile="full",
            telegram_bot_token=SecretStr("123456:token"),
            target_chat_id=-1001,
            schedule_topic_id=10,
            materials_topic_id=11,
            headman_user_id=12,
            spbgasu_group_id="42",
            backup_status_path="/var/lib/szs-hub-backup/last-success",
            _env_file=None,
        )


def test_non_local_schedule_only_needs_no_personal_identifiers_or_backup() -> None:
    settings = Settings(
        app_env="production",
        feature_profile="schedule_only",
        telegram_bot_token=SecretStr("123456:token"),
        target_chat_id=-1001,
        schedule_topic_id=10,
        spbgasu_group_id="42",
        _env_file=None,
    )

    assert settings.headman_user_id is None
    assert settings.alert_user_id is None
    assert settings.backup_status_path is None


def test_alert_recipient_must_be_a_positive_user_identifier() -> None:
    with pytest.raises(ValidationError, match="alert_user_id must be a positive"):
        Settings(alert_user_id=-1, _env_file=None)


def test_non_local_upstreams_require_https() -> None:
    with pytest.raises(ValidationError, match="spbgasu_base_url.*HTTPS"):
        Settings(app_env="staging", spbgasu_base_url="http://schedule.test", _env_file=None)
    with pytest.raises(ValidationError, match="ai_base_url.*HTTPS"):
        Settings(app_env="production", ai_base_url="http://ai.test", _env_file=None)
    with pytest.raises(ValidationError, match="schedule_calendar_url.*HTTPS"):
        Settings(
            app_env="production",
            schedule_calendar_url="http://calendar.test/feed.ics",
            _env_file=None,
        )


def test_local_development_can_use_cleartext_test_upstreams() -> None:
    settings = Settings(
        spbgasu_base_url="http://schedule.test",
        ai_base_url="http://ai.test",
        _env_file=None,
    )
    assert settings.spbgasu_base_url.startswith("http://")
