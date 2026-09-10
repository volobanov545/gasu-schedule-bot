from __future__ import annotations

from pathlib import Path

from szs_hub.alerting import AlertCode

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = PROJECT_ROOT / "deploy" / "systemd"


def test_failure_units_have_one_exact_alert_mapping() -> None:
    expected = {
        "szs-hub.service": AlertCode.MAIN,
        "szs-hub-health.service": AlertCode.HEALTH,
        "szs-hub-backup.service": AlertCode.BACKUP,
        "szs-hub-migrate.service": AlertCode.MIGRATE,
    }

    for filename, code in expected.items():
        unit = (SYSTEMD / filename).read_text(encoding="utf-8")
        assert unit.count("OnFailure=") == 1
        assert f"OnFailure=szs-hub-alert@{code.value}.service" in unit


def test_alert_template_is_non_recursive_bounded_and_hardened() -> None:
    unit = (SYSTEMD / "szs-hub-alert@.service").read_text(encoding="utf-8")

    assert "OnFailure=" not in unit
    assert "ExecStart=/opt/szs-hub/current/.venv/bin/szs-hub alert --code %i" in unit
    assert "EnvironmentFile=/etc/szs-hub/runtime.env" in unit
    assert "Restart=on-failure" in unit
    assert "RestartSec=15min" in unit
    assert "StartLimitIntervalSec=6h" in unit
    assert "StartLimitBurst=24" in unit
    assert "TimeoutStartSec=30s" in unit
    assert "StateDirectory=szs-hub-alert" in unit
    assert "StateDirectoryMode=0700" in unit
    assert "NoNewPrivileges=yes" in unit
    assert "ProtectSystem=strict" in unit
    assert "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6" in unit


def test_timers_and_alert_template_do_not_create_alert_cycles() -> None:
    for filename in (
        "szs-hub-health.timer",
        "szs-hub-backup.timer",
        "szs-hub-alert@.service",
    ):
        assert "OnFailure=" not in (SYSTEMD / filename).read_text(encoding="utf-8")
