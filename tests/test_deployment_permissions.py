from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_runtime_secrets_are_installed_root_only() -> None:
    example = (PROJECT_ROOT / "deploy" / "runtime.env.example").read_text(encoding="utf-8")
    guide = (PROJECT_ROOT / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8")

    assert "Owner: root:root; mode: 0600" in example
    assert "sudo install -o root -g root -m 0600" in guide
    assert "sudo install -o root -g szshub-state -m 0640" not in guide
