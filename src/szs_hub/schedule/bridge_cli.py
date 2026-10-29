"""Lightweight GitVerse entrypoint using the canonical parser and envelope."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass

from szs_hub.publisher import dispatch_tomorrow
from szs_hub.schedule.spbgasu import SpbGasuClient


@dataclass(frozen=True, slots=True)
class BridgeSettings:
    spbgasu_group_id: str | None
    spbgasu_base_url: str
    timezone: str

    @classmethod
    def from_environment(cls) -> BridgeSettings:
        return cls(
            spbgasu_group_id=os.environ.get("SPBGASU_GROUP_ID"),
            spbgasu_base_url=os.environ.get("SPBGASU_BASE_URL", "https://rasp.spbgasu.ru"),
            timezone=os.environ.get("TIMEZONE", "Europe/Moscow"),
        )


async def _probe(settings: BridgeSettings) -> str:
    group_key = (settings.spbgasu_group_id or "").strip()
    if not group_key:
        raise ValueError("SPBGASU_GROUP_ID не задан")
    client = SpbGasuClient(base_url=settings.spbgasu_base_url)
    try:
        return await client.probe_group_schema(group_key)
    finally:
        await client.aclose()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Получить расписание и передать в GitHub")
    parser.add_argument("--force-digest", action="store_true")
    parser.add_argument("--probe-schema", action="store_true")
    args = parser.parse_args(argv)
    settings = BridgeSettings.from_environment()
    probe_schema = args.probe_schema or os.environ.get("SPBGASU_PROBE_SCHEMA") == "true"
    force_digest = args.force_digest or os.environ.get("SCHEDULE_FORCE_DIGEST") == "true"
    try:
        if probe_schema:
            print(asyncio.run(_probe(settings)))
        else:
            asyncio.run(
                dispatch_tomorrow(
                    settings,
                    github_token=os.environ.get("BRIDGE_GH_TOKEN", ""),
                    github_repository=os.environ.get("BRIDGE_GH_REPOSITORY", ""),
                    force_digest=force_digest,
                )
            )
            print("Карточка передана в GitHub.")
    except Exception as exc:
        print(f"Расписание не передано: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
