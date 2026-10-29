from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from szs_hub import personal_sender, publisher
from szs_hub.config import Settings

_BUNDLE = json.dumps({
    "user_id": 123, "chat_id": -1001, "topic_id": 42, "api_id": 42,
    "api_hash": "a" * 32, "session": "private-test-session-never-log" * 8,
    "access_hash": "123456789",
})


class FakeStdin:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def write(self, data: bytes) -> None:
        self.requests.append(json.loads(data))

    async def drain(self) -> None:
        pass


class FakeStdout:
    def __init__(self, process: FakeProcess, responses: list[object]) -> None:
        self.process = process
        self.responses = responses

    async def readline(self) -> bytes:
        request = self.process.stdin.requests[-1]
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, BaseException):
                raise response
            if isinstance(response, bytes):
                return response
            if isinstance(response, dict):
                return json.dumps(response).encode() + b"\n"
            return json.dumps({"id": request["id"], "ok": True, "result": response}).encode()
        return json.dumps({"id": request["id"], "ok": True, "result": None}).encode()


class FakeProcess:
    def __init__(self, responses: list[object]) -> None:
        self.stdin = FakeStdin()
        self.stdout = FakeStdout(self, responses)
        self.returncode: int | None = None
        self.kill_count = 0
        self.wait_count = 0

    def kill(self) -> None:
        self.kill_count += 1
        self.returncode = -9

    async def wait(self) -> int:
        self.wait_count += 1
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


def _destination() -> personal_sender.PersonalScheduleDestination:
    return personal_sender.PersonalScheduleDestination(_BUNDLE, chat_id=-1001, topic_id=42)


def _fake_child(
    monkeypatch: pytest.MonkeyPatch, responses: list[object],
) -> tuple[FakeProcess, list[tuple[tuple[object, ...], dict[str, Any]]]]:
    process = FakeProcess(responses)
    launches: list[tuple[tuple[object, ...], dict[str, Any]]] = []

    async def launch(*args: object, **kwargs: Any) -> FakeProcess:
        launches.append((args, kwargs))
        return process

    monkeypatch.setattr(personal_sender.shutil, "which", lambda _: "node-for-test")
    monkeypatch.setattr(personal_sender.asyncio, "create_subprocess_exec", launch)
    return process, launches


@pytest.mark.parametrize("raw", [
    "not-json-private-session", "[]", "{}", "null",
    json.dumps({"user_id": True, "chat_id": -1001, "topic_id": 42}),
    json.dumps({"user_id": 0, "chat_id": -1001, "topic_id": 42}),
    json.dumps({"user_id": 123, "chat_id": -1002, "topic_id": 42}),
    json.dumps({"user_id": 123, "chat_id": -1001, "topic_id": 43}),
])
def test_constructor_rejects_malformed_or_differently_scoped_credentials(raw: str) -> None:
    with pytest.raises(ValueError, match="do not match the configured target") as raised:
        personal_sender.PersonalScheduleDestination(raw, chat_id=-1001, topic_id=42)

    assert raw not in str(raised.value)


@pytest.mark.asyncio
async def test_jsonl_requests_have_monotonic_ids_and_credentials_are_not_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process, launches = _fake_child(monkeypatch, ["user:123", 101, 102, 102, None, None])
    destination = _destination()

    await destination.prepare()
    assert await destination.send(chat_id=-1001, topic_id=42, text="<b>Пара</b>") == 101
    assert await destination.send_rich(
        chat_id=-1001, topic_id=42, rich_html="<table>test</table>",
        fallback_html="unused", silent=True,
    ) == 102
    assert await destination.edit_rich(
        chat_id=-1001, message_id=102, rich_html="<details>test</details>", fallback_html="unused",
    ) == 102
    await destination.pin_message(chat_id=-1001, message_id=102)
    await destination.aclose()

    assert destination.card_key == "user:123:-1001:42"
    requests = process.stdin.requests
    assert [row["id"] for row in requests] == [1, 2, 3, 4, 5, 6]
    assert [row["method"] for row in requests] == [
        "preflight", "send", "send_rich", "edit_rich", "pin_message", "close",
    ]
    assert requests[2]["params"] == {
        "chat_id": -1001, "topic_id": 42, "rich_html": "<table>test</table>", "silent": True,
    }
    assert "fallback_html" not in json.dumps(requests)
    assert _BUNDLE not in json.dumps(requests)
    assert len(launches) == 1
    args, options = launches[0]
    assert _BUNDLE not in str(args)
    assert options["env"]["TELEGRAM_USER_CREDENTIALS"] == _BUNDLE
    assert options["stderr"] == personal_sender.asyncio.subprocess.DEVNULL
    assert destination._process is None
    assert process.wait_count >= 1


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, True, False, 0, -1, "102", {"id": 102}])
async def test_invalid_rpc_message_id_is_not_accepted(
    monkeypatch: pytest.MonkeyPatch, value: object,
) -> None:
    process, _ = _fake_child(monkeypatch, [{"id": 1, "ok": True, "result": value}])
    destination = _destination()

    with pytest.raises(RuntimeError, match="invalid message ID"):
        await destination.send(chat_id=-1001, topic_id=42, text="test")
    await destination.aclose()

    assert process.wait_count >= 1
    assert destination._process is None


@pytest.mark.asyncio
@pytest.mark.parametrize("code,expected", [
    ("MESSAGE_NOT_FOUND", "message to edit not found"),
    ("MESSAGE_AUTHOR_REQUIRED", "personal Telegram: MESSAGE_AUTHOR_REQUIRED"),
    ("FLOOD_WAIT_19", "personal Telegram: FLOOD_WAIT_19"),
    ("MESSAGE_EDIT_TIME_EXPIRED", "personal Telegram: MESSAGE_EDIT_TIME_EXPIRED"),
    ("SECRET-private-value", "personal Telegram: DELIVERY_FAILED"),
    ({"session": "private"}, "personal Telegram: DELIVERY_FAILED"),
    (None, "personal Telegram: DELIVERY_FAILED"),
])
async def test_only_confirmed_missing_message_maps_to_replacement_error(
    monkeypatch: pytest.MonkeyPatch, code: object, expected: str,
) -> None:
    process, _ = _fake_child(monkeypatch, [{"id": 1, "ok": False, "error": code}])
    destination = _destination()

    with pytest.raises(RuntimeError) as raised:
        await destination.edit_rich(
            chat_id=-1001, message_id=10, rich_html="<p>test</p>", fallback_html="unused",
        )

    assert str(raised.value) == expected
    assert _BUNDLE not in str(raised.value)
    assert "SECRET-private-value" not in str(raised.value)
    assert len(process.stdin.requests) == 1  # No retry or plain-text fallback.
    await destination.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    b'not-json-private-test-session\n',
    {"id": 999, "ok": False, "error": "private-test-session"},
    ["private-test-session"],
    TimeoutError(),
])
async def test_bad_or_timed_out_transport_is_closed_without_leaking_raw_response(
    monkeypatch: pytest.MonkeyPatch, response: object,
) -> None:
    # A list is JSON, but not a valid protocol response.
    if isinstance(response, list):
        response = json.dumps(response).encode()
    process, _ = _fake_child(monkeypatch, [response])
    destination = _destination()

    with pytest.raises(RuntimeError) as raised:
        await destination.send(chat_id=-1001, topic_id=42, text="test")

    assert "private-test-session" not in str(raised.value)
    assert process.kill_count == 1
    assert process.wait_count == 1
    assert destination._process is None
    await destination.aclose()
    assert process.kill_count == 1


@pytest.mark.asyncio
async def test_prepare_identity_mismatch_closes_child_without_sending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process, _ = _fake_child(monkeypatch, ["user:456", None])
    destination = _destination()

    with pytest.raises(RuntimeError, match="identity mismatch"):
        await destination.prepare()

    assert [row["method"] for row in process.stdin.requests] == ["preflight", "close"]
    assert destination._process is None
    assert process.wait_count >= 1


@pytest.mark.asyncio
async def test_missing_node_fails_before_start_and_unused_close_does_not_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, launches = _fake_child(monkeypatch, [])
    monkeypatch.setattr(personal_sender.shutil, "which", lambda _: None)
    destination = _destination()
    await destination.aclose()

    with pytest.raises(RuntimeError, match="requires Node.js"):
        await destination.prepare()

    assert launches == []
    assert destination._process is None


@pytest.mark.asyncio
async def test_close_error_still_kills_and_reaps_child(monkeypatch: pytest.MonkeyPatch) -> None:
    process, _ = _fake_child(monkeypatch, [
        "user:123", {"id": 2, "ok": False, "error": "TELEGRAM_REQUEST_FAILED"},
    ])
    destination = _destination()
    await destination.prepare()

    await destination.aclose()
    await destination.aclose()

    assert process.kill_count == 1
    assert process.wait_count == 1
    assert destination._process is None


def test_factory_selects_user_only_by_explicit_opt_in_without_starting_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(personal_sender.shutil, "which", lambda _: None)
    settings = Settings(
        schedule_sender="user", telegram_user_credentials=SecretStr(_BUNDLE),
        target_chat_id=-1001, schedule_topic_id=42, _env_file=None,
    )

    destination = publisher._make_destination(settings)

    assert isinstance(destination, personal_sender.PersonalScheduleDestination)
    assert destination.card_key == "user:123:-1001:42"
    assert destination._process is None


@pytest.mark.asyncio
async def test_factory_ignores_personal_secret_in_default_bot_mode() -> None:
    settings = Settings(
        telegram_user_credentials=SecretStr(_BUNDLE), telegram_bot_token=SecretStr("123:test"),
        target_chat_id=-1001, schedule_topic_id=42, _env_file=None,
    )

    destination = publisher._make_destination(settings)

    assert isinstance(destination, publisher.TelegramBotApiDestination)
    await destination.aclose()


def test_preflight_is_manual_read_only_and_shares_sender_serialization() -> None:
    workflows = Path(__file__).parents[1] / ".github" / "workflows"
    preflight = (workflows / "check-personal-sender.yml").read_text(encoding="utf-8")

    assert "workflow_dispatch:" in preflight
    assert "schedule:" not in preflight
    assert "run: szs-hub check-personal-sender" in preflight
    assert "publish-dispatched" not in preflight
    assert "publish-reminder" not in preflight
    assert "upload-artifact" not in preflight
    assert "actions/cache" not in preflight
    assert "contents: read" in preflight
    for name in ("check-personal-sender.yml", "receive-schedule.yml", "remind-schedule.yml"):
        content = (workflows / name).read_text(encoding="utf-8")
        assert "group: szs-schedule-state" in content
        assert "cancel-in-progress: false" in content
    for name in ("receive-schedule.yml", "remind-schedule.yml"):
        content = (workflows / name).read_text(encoding="utf-8")
        assert "SCHEDULE_SENDER: ${{ vars.SCHEDULE_SENDER || 'bot' }}" in content
        assert "if: env.SCHEDULE_SENDER == 'user'" in content
        assert "path: .schedule-state" in content
        assert "path: tools/telegram-personal" not in content
