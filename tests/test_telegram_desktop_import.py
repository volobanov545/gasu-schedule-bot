from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from szs_hub.archive.telegram_desktop import (
    ALBUM_IMPORT_LIMITATION,
    TOPIC_IMPORT_LIMITATION,
    normalize_telegram_desktop_chat_id,
    plan_telegram_desktop_import,
)


def test_export_loader_rejects_oversize_json_before_parsing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from szs_hub.archive import telegram_desktop

    export = tmp_path / "result.json"
    export.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(telegram_desktop, "MAX_EXPORT_JSON_BYTES", 1)

    with pytest.raises(ValueError, match="256 MiB"):
        telegram_desktop.load_telegram_desktop_export(export)


RAW_CHAT_ID = 1_234_567_890
TARGET_CHAT_ID = -1_001_234_567_890


def export_data() -> dict[str, object]:
    return {
        "name": "Test group",
        "type": "private_supergroup",
        "id": RAW_CHAT_ID,
        "messages": [
            {
                "id": 10,
                "type": "message",
                "date_unixtime": "1788264000",
                "edited": "2026-09-01T15:01:00+03:00",
                "edited_unixtime": "1788264060",
                "from": "Участник",
                "from_id": "user777",
                "text": [
                    "Вот ",
                    {"type": "bold", "text": "методичка"},
                ],
                "file": "files/manual.pdf",
                "file_name": "manual.pdf",
                "mime_type": "application/pdf",
                "file_size": 1234,
                "reply_to_message_id": 9,
            },
            {
                "id": 11,
                "type": "message",
                "date": "2026-09-01T13:05:00+03:00",
                "from": "Канал группы",
                "from_id": "channel888",
                "text": "Фото с доски",
                "photo": "photos/photo_1.jpg",
                "media_type": "photo",
            },
        ],
    }


def test_import_preserves_ids_text_replies_and_media(tmp_path: Path) -> None:
    plan = plan_telegram_desktop_import(export_data(), export_root=tmp_path)

    first = plan.messages[0]
    assert first.unique_key == (TARGET_CHAT_ID, 10)
    assert first.sender_id == 777
    assert first.edited_at == datetime.fromtimestamp(1788264060, tz=UTC)
    assert first.text == "Вот методичка"
    assert first.reply_to_message_id == 9
    assert first.file_path == (tmp_path / "files" / "manual.pdf").resolve()
    assert first.sent_at == datetime.fromtimestamp(1788264000, tz=UTC)
    assert plan.report.messages_planned == 2
    assert plan.report.documents == 1
    assert plan.report.photos == 1
    assert plan.report.raw_chat_id == RAW_CHAT_ID
    assert plan.report.chat_type == "private_supergroup"
    assert plan.report.normalized_chat_id == TARGET_CHAT_ID
    assert plan.report.users == 1
    assert plan.messages[1].sender_reference == "channel888"
    assert plan.messages[1].sender_id is None
    assert plan.report.topics == 0
    assert plan.report.media_groups == 0
    assert plan.report.limitations == (TOPIC_IMPORT_LIMITATION, ALBUM_IMPORT_LIMITATION)


def test_repeated_import_skips_existing_keys(tmp_path: Path) -> None:
    plan = plan_telegram_desktop_import(
        export_data(),
        export_root=tmp_path,
        existing_keys={(TARGET_CHAT_ID, 10)},
    )

    assert [message.message_id for message in plan.messages] == [11]
    assert plan.report.duplicates_skipped == 1


def test_malformed_objects_are_reported_without_aborting_batch(tmp_path: Path) -> None:
    data = export_data()
    messages = data["messages"]
    assert isinstance(messages, list)
    messages.append({"id": 12, "date": "not-a-date", "text": "bad"})

    plan = plan_telegram_desktop_import(data, export_root=tmp_path)

    assert plan.report.messages_found == 3
    assert plan.report.messages_planned == 2
    assert plan.report.failures[0].message_id == 12


def test_media_path_traversal_is_rejected(tmp_path: Path) -> None:
    data = export_data()
    messages = data["messages"]
    assert isinstance(messages, list)
    first = messages[0]
    assert isinstance(first, dict)
    first["file"] = "../secret.txt"

    plan = plan_telegram_desktop_import(data, export_root=tmp_path)

    assert [message.message_id for message in plan.messages] == [11]
    assert "escapes" in plan.report.failures[0].reason


@pytest.mark.parametrize(
    ("chat_type", "expected"),
    [
        ("private_supergroup", -1_001_234_567_890),
        ("public_supergroup", -1_001_234_567_890),
        ("private_channel", -1_001_234_567_890),
        ("public_channel", -1_001_234_567_890),
        ("private_group", -1_234_567_890),
    ],
)
def test_real_export_peer_types_normalize_to_bot_api_ids(
    chat_type: str,
    expected: int,
) -> None:
    assert normalize_telegram_desktop_chat_id(RAW_CHAT_ID, chat_type) == expected


@pytest.mark.parametrize("chat_type", ["personal_chat", "bot_chat", "unknown", ""])
def test_non_group_export_types_fail_closed(chat_type: str) -> None:
    with pytest.raises(ValueError, match="unsupported|missing"):
        plan_telegram_desktop_import(
            {"id": RAW_CHAT_ID, "type": chat_type, "messages": []},
            export_root=Path.cwd(),
        )


def test_non_positive_bare_chat_id_fails_closed(tmp_path: Path) -> None:
    data = export_data()
    data["id"] = TARGET_CHAT_ID

    with pytest.raises(ValueError, match="positive bare id"):
        plan_telegram_desktop_import(data, export_root=tmp_path)


def test_edited_iso_timestamp_is_used_when_raw_timestamp_is_absent(tmp_path: Path) -> None:
    data = export_data()
    messages = data["messages"]
    assert isinstance(messages, list)
    first = messages[0]
    assert isinstance(first, dict)
    first.pop("edited_unixtime")

    message = plan_telegram_desktop_import(data, export_root=tmp_path).messages[0]

    assert message.edited_at == datetime.fromisoformat("2026-09-01T15:01:00+03:00")
