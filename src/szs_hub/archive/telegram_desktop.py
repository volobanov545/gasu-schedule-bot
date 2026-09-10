"""Fail-safe, idempotency-aware parser for Telegram Desktop JSON exports."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SENDER_ID = re.compile(r"user(\d+)$")
_BOT_API_CHANNEL_OFFSET = 1_000_000_000_000
MAX_EXPORT_JSON_BYTES = 256 * 1024 * 1024
_CHANNEL_DIALOG_TYPES = frozenset(
    {
        "private_channel",
        "private_supergroup",
        "public_channel",
        "public_supergroup",
    }
)
_GROUP_DIALOG_TYPES = frozenset({"private_group"})

TOPIC_IMPORT_LIMITATION = (
    "Telegram Desktop JSON does not reliably expose topic identifiers; "
    "only explicit topic_id/message_thread_id values are preserved and no inference is performed"
)
ALBUM_IMPORT_LIMITATION = (
    "Telegram Desktop JSON does not reliably expose media-group identifiers; "
    "only explicit media_group_id values are preserved and no inference is performed"
)


@dataclass(frozen=True, slots=True)
class ImportedMessage:
    chat_id: int
    message_id: int
    sent_at: datetime
    sender_id: int | None
    sender_reference: str | None
    sender_display_name: str | None
    text: str
    message_type: str
    edited_at: datetime | None = None
    reply_to_message_id: int | None = None
    topic_id: int | None = None
    media_group_id: str | None = None
    file_path: Path | None = None
    filename: str | None = None
    mime_type: str | None = None
    file_size: int | None = None
    forwarded_from: str | None = None

    def __post_init__(self) -> None:
        if self.sent_at.tzinfo is None:
            raise ValueError("imported message timestamp must be timezone-aware")
        if self.edited_at is not None and self.edited_at.tzinfo is None:
            raise ValueError("imported message edit timestamp must be timezone-aware")

    @property
    def unique_key(self) -> tuple[int, int]:
        return self.chat_id, self.message_id


@dataclass(frozen=True, slots=True)
class ImportFailure:
    message_id: int | None
    reason: str


@dataclass(frozen=True, slots=True)
class ImportReport:
    raw_chat_id: int
    chat_type: str
    normalized_chat_id: int
    messages_found: int
    messages_planned: int
    duplicates_skipped: int
    users: int
    documents: int
    photos: int
    videos: int
    topics: int
    media_groups: int
    failures: tuple[ImportFailure, ...]
    date_from: datetime | None
    date_to: datetime | None
    limitations: tuple[str, ...]

    @property
    def chat_id(self) -> int:
        """Backward-compatible name for the canonical Bot API chat identifier."""

        return self.normalized_chat_id


@dataclass(frozen=True, slots=True)
class ImportPlan:
    messages: tuple[ImportedMessage, ...]
    report: ImportReport


def load_telegram_desktop_export(path: Path) -> Mapping[str, Any]:
    """Load an owner-provided Telegram Desktop `result.json`."""

    if path.stat().st_size > MAX_EXPORT_JSON_BYTES:
        raise ValueError("Telegram export JSON exceeds the safe 256 MiB inspection limit")
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except RecursionError as exc:
        raise ValueError("Telegram export JSON is nested too deeply") from exc
    if not isinstance(data, dict):
        raise ValueError("Telegram export root must be a JSON object")
    return data


def plan_telegram_desktop_import(
    data: Mapping[str, Any],
    *,
    export_root: Path,
    existing_keys: Iterable[tuple[int, int]] = (),
) -> ImportPlan:
    """Parse records and remove known duplicates without mutating a database."""

    raw_chat_id = _required_int(data.get("id"), "export chat id")
    chat_type = _required_str(data.get("type"), "export chat type")
    chat_id = normalize_telegram_desktop_chat_id(raw_chat_id, chat_type)
    raw_messages = data.get("messages")
    if not isinstance(raw_messages, list):
        raise ValueError("Telegram export does not contain a messages array")

    seen = set(existing_keys)
    planned: list[ImportedMessage] = []
    failures: list[ImportFailure] = []
    duplicate_count = 0
    senders: set[int] = set()
    type_counts: Counter[str] = Counter()
    topics: set[int] = set()
    media_groups: set[str] = set()

    for raw in raw_messages:
        if not isinstance(raw, dict):
            failures.append(ImportFailure(None, "message is not an object"))
            continue
        raw_id = _optional_int(raw.get("id"))
        try:
            message = _parse_message(raw, chat_id=chat_id, export_root=export_root)
        except (TypeError, ValueError) as exc:
            failures.append(ImportFailure(raw_id, str(exc)))
            continue

        if message.unique_key in seen:
            duplicate_count += 1
            continue
        seen.add(message.unique_key)
        planned.append(message)
        if message.sender_id is not None:
            senders.add(message.sender_id)
        type_counts[message.message_type] += 1
        if message.topic_id is not None:
            topics.add(message.topic_id)
        if message.media_group_id:
            media_groups.add(message.media_group_id)

    dates = [message.sent_at for message in planned]
    report = ImportReport(
        raw_chat_id=raw_chat_id,
        chat_type=chat_type,
        normalized_chat_id=chat_id,
        messages_found=len(raw_messages),
        messages_planned=len(planned),
        duplicates_skipped=duplicate_count,
        users=len(senders),
        documents=type_counts["file"],
        photos=type_counts["photo"],
        videos=type_counts["video_file"],
        topics=len(topics),
        media_groups=len(media_groups),
        failures=tuple(failures),
        date_from=min(dates) if dates else None,
        date_to=max(dates) if dates else None,
        limitations=(TOPIC_IMPORT_LIMITATION, ALBUM_IMPORT_LIMITATION),
    )
    return ImportPlan(tuple(planned), report)


def _parse_message(
    raw: Mapping[str, Any],
    *,
    chat_id: int,
    export_root: Path,
) -> ImportedMessage:
    message_id = _required_int(raw.get("id"), "message id")
    sent_at = _parse_datetime(raw)
    sender_reference = _optional_str(raw.get("from_id"))
    sender_id = _sender_id(sender_reference)
    message_type = _message_type(raw)
    file_path = _media_path(raw, export_root)
    file_size = _optional_int(raw.get("file_size"))
    if file_size is not None and file_size < 0:
        raise ValueError("file size cannot be negative")

    return ImportedMessage(
        chat_id=chat_id,
        message_id=message_id,
        sent_at=sent_at,
        sender_id=sender_id,
        sender_reference=sender_reference,
        sender_display_name=_optional_str(raw.get("from")),
        text=_flatten_text(raw.get("text")),
        message_type=message_type,
        edited_at=_parse_edited_datetime(raw),
        reply_to_message_id=_optional_int(raw.get("reply_to_message_id")),
        topic_id=_optional_int(raw.get("topic_id") or raw.get("message_thread_id")),
        media_group_id=_optional_str(raw.get("media_group_id")),
        file_path=file_path,
        filename=_optional_str(raw.get("file_name")) or (file_path.name if file_path else None),
        mime_type=_optional_str(raw.get("mime_type")),
        file_size=file_size,
        forwarded_from=_forwarded_from(raw.get("forwarded_from")),
    )


def _parse_datetime(raw: Mapping[str, Any]) -> datetime:
    unix_value = raw.get("date_unixtime")
    if unix_value not in (None, ""):
        try:
            return datetime.fromtimestamp(int(str(unix_value)), tz=UTC)
        except (ValueError, OSError, OverflowError) as exc:
            raise ValueError("invalid date_unixtime") from exc

    value = raw.get("date")
    if not isinstance(value, str) or not value:
        raise ValueError("message has no valid date")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid message date") from exc
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _parse_edited_datetime(raw: Mapping[str, Any]) -> datetime | None:
    unix_value = raw.get("edited_unixtime")
    if unix_value not in (None, ""):
        try:
            return datetime.fromtimestamp(int(str(unix_value)), tz=UTC)
        except (ValueError, OSError, OverflowError) as exc:
            raise ValueError("invalid edited_unixtime") from exc

    value = raw.get("edited")
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError("invalid edited date")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid edited date") from exc
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _flatten_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        text = value.get("text")
        return text if isinstance(text, str) else ""
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return "".join(_flatten_text(item) for item in value)
    return ""


def _message_type(raw: Mapping[str, Any]) -> str:
    media_type = _optional_str(raw.get("media_type"))
    if media_type:
        return media_type
    if raw.get("photo"):
        return "photo"
    if raw.get("file"):
        return "file"
    return _optional_str(raw.get("type")) or "message"


def _media_path(raw: Mapping[str, Any], export_root: Path) -> Path | None:
    value = raw.get("file") or raw.get("photo")
    if not isinstance(value, str) or not value:
        return None
    relative = Path(value)
    if relative.is_absolute():
        raise ValueError("media path must be relative to the export")
    root = export_root.resolve()
    resolved = (root / relative).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("media path escapes the export directory")
    return resolved


def _sender_id(reference: str | None) -> int | None:
    if not reference:
        return None
    match = _SENDER_ID.fullmatch(reference)
    if match is None:
        return None
    value = int(match.group(1))
    return value if value > 0 else None


def normalize_telegram_desktop_chat_id(raw_chat_id: int, chat_type: str) -> int:
    """Convert Telegram Desktop's positive bare peer ID to a Bot API chat ID.

    Telegram Desktop identifies the peer type separately and serializes its bare
    positive ID.  Guessing from the number alone is unsafe, so unsupported peer
    types and non-positive bare identifiers are rejected.
    """

    if isinstance(raw_chat_id, bool) or raw_chat_id <= 0:
        raise ValueError("Telegram Desktop export chat id must be a positive bare id")
    if chat_type in _CHANNEL_DIALOG_TYPES:
        return -(_BOT_API_CHANNEL_OFFSET + raw_chat_id)
    if chat_type in _GROUP_DIALOG_TYPES:
        return -raw_chat_id
    raise ValueError(f"unsupported Telegram Desktop export chat type: {chat_type}")


def _forwarded_from(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        for key in ("name", "from", "id"):
            candidate = value.get(key)
            if isinstance(candidate, str):
                return candidate
    return None


def _required_int(value: object, label: str) -> int:
    result = _optional_int(value)
    if result is None:
        raise ValueError(f"{label} is missing or invalid")
    return result


def _required_str(value: object, label: str) -> str:
    result = _optional_str(value)
    if result is None:
        raise ValueError(f"{label} is missing or invalid")
    return result


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
