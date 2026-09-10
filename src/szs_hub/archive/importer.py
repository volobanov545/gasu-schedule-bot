"""Durable persistence for owner-provided Telegram Desktop exports.

Parsing and dry-run reporting intentionally stay in :mod:`telegram_desktop`.
This module applies an already validated export to the canonical database in
small transactions.  Every committed batch advances the matching ``ImportRun``
cursor in the same transaction, so an interrupted run can safely resume.

The importer never opens, copies, deletes, or uploads exported media.  A media
path is stored as an explicitly relative export reference; opaque identifiers
with the ``telegram-export-local:`` prefix must not be sent to the Bot API.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import stat
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from szs_hub.archive.indexing import upsert_message_search_document
from szs_hub.archive.telegram_desktop import (
    ImportedMessage,
    ImportFailure,
    ImportPlan,
    load_telegram_desktop_export,
    plan_telegram_desktop_import,
)
from szs_hub.storage import (
    File,
    ImportRun,
    MediaGroup,
    Message,
    SearchDocument,
    Topic,
    User,
    utc_now,
)

_FINGERPRINT_PREFIX: Final = "sha256:"
_LOCAL_FILE_ID_PREFIX: Final = "telegram-export-local:"
_STORAGE_PATH_PREFIX: Final = "telegram-export-relative:"
_STATS_VERSION: Final = 2
_MAX_REPORTED_MEDIA: Final = 1_000


class ExportChatMismatchError(ValueError):
    """Raised before persistence when an export belongs to another chat."""


class ExportFingerprintMismatchError(ValueError):
    """Raised before persistence when the reviewed export bytes changed."""


@dataclass(frozen=True, slots=True)
class MissingMedia:
    """A safe, relative reference whose regular file is absent from the export."""

    message_id: int
    relative_path: str
    reason: str


@dataclass(frozen=True, slots=True)
class PersistentImportReport:
    """Terminal summary for one durable import invocation."""

    import_run_id: int
    source_fingerprint: str
    status: str
    raw_chat_id: int
    chat_type: str
    normalized_chat_id: int
    messages_found: int
    messages_planned: int
    messages_processed: int
    messages_created: int
    messages_updated: int
    users_created: int
    topics_created: int
    media_groups_created: int
    files_created: int
    parser_failures: tuple[ImportFailure, ...]
    missing_media: tuple[MissingMedia, ...]
    limitations: tuple[str, ...]
    resumed_from: int
    already_completed: bool

    @property
    def chat_id(self) -> int:
        """Backward-compatible alias for the canonical Bot API chat identifier."""

        return self.normalized_chat_id


@dataclass(frozen=True, slots=True)
class _PersistResult:
    message_created: bool
    user_created: bool
    topic_created: bool
    media_group_created: bool
    file_created: bool


@dataclass(frozen=True, slots=True)
class TelegramDesktopImportPreflight:
    """Read-only inspection result for one exact Telegram Desktop export file."""

    resolved_path: Path = field(repr=False)
    source_fingerprint: str
    plan: ImportPlan
    missing_media: tuple[MissingMedia, ...]


@dataclass(frozen=True, slots=True)
class ImportReconciliation:
    """Database evidence that every planned object has its durable projections."""

    messages_planned: int
    messages_archived: int
    search_documents_verified: int
    files_expected: int
    files_archived: int
    reply_links_checked: int
    reply_link_mismatches: int

    @property
    def complete(self) -> bool:
        return (
            self.messages_archived == self.messages_planned
            and self.search_documents_verified == self.messages_planned
            and self.files_archived == self.files_expected
            and self.reply_link_mismatches == 0
        )


def telegram_export_fingerprint(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Return a path-independent fingerprint of the exact JSON source bytes."""

    if chunk_size < 4096:
        raise ValueError("fingerprint chunk size must be at least 4096 bytes")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(chunk_size), b""):
            digest.update(block)
    return _FINGERPRINT_PREFIX + digest.hexdigest()


class TelegramDesktopImporter:
    """Persist one Telegram Desktop JSON export into a configured group archive."""

    def __init__(self, *, target_chat_id: int, batch_size: int = 200) -> None:
        if not 1 <= batch_size <= 1_000:
            raise ValueError("import batch size must be between 1 and 1000")
        self._target_chat_id = target_chat_id
        self._batch_size = batch_size

    async def run(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        export_path: Path,
        *,
        expected_fingerprint: str | None = None,
    ) -> PersistentImportReport:
        """Import or resume ``export_path`` using bounded, independently committed batches."""

        prepared = await asyncio.to_thread(inspect_telegram_desktop_export, export_path)
        resolved = prepared.resolved_path
        fingerprint = prepared.source_fingerprint
        if expected_fingerprint is not None and not hmac.compare_digest(
            fingerprint,
            expected_fingerprint,
        ):
            raise ExportFingerprintMismatchError(
                "Telegram export fingerprint differs from the reviewed plan"
            )
        plan = prepared.plan
        if plan.report.chat_id != self._target_chat_id:
            raise ExportChatMismatchError(
                "Telegram export chat does not match the configured target chat"
            )
        missing_media = prepared.missing_media
        run, resumed_from, already_completed = await self._prepare_run(
            session_factory,
            fingerprint=fingerprint,
            plan=plan,
            missing_media=missing_media,
        )
        if already_completed:
            return _build_report(
                run,
                fingerprint=fingerprint,
                plan=plan,
                missing_media=missing_media,
                resumed_from=resumed_from,
                already_completed=True,
            )

        try:
            for start in range(resumed_from, len(plan.messages), self._batch_size):
                batch = plan.messages[start : start + self._batch_size]
                async with session_factory() as session, session.begin():
                    persisted_run = await session.get(ImportRun, run.id)
                    if persisted_run is None:
                        raise RuntimeError("import run disappeared during processing")
                    stats = _normalized_stats(persisted_run.stats, plan)
                    for message in batch:
                        result = await self._persist_message(
                            session,
                            message,
                            export_root=resolved.parent,
                        )
                        _record_result(stats, result)
                    stats["next_index"] = start + len(batch)
                    stats["batches_completed"] = int(stats["batches_completed"]) + 1
                    persisted_run.stats = stats
                    persisted_run.status = "running"

            async with session_factory() as session, session.begin():
                persisted_run = await session.get(ImportRun, run.id)
                if persisted_run is None:
                    raise RuntimeError("import run disappeared before completion")
                persisted_run.status = "succeeded"
                persisted_run.completed_at = utc_now()
                persisted_run.error = None
                run = persisted_run
        except Exception as exc:
            await _mark_failed(session_factory, run.id, exc)
            raise

        return _build_report(
            run,
            fingerprint=fingerprint,
            plan=plan,
            missing_media=missing_media,
            resumed_from=resumed_from,
            already_completed=False,
        )

    async def _prepare_run(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        fingerprint: str,
        plan: ImportPlan,
        missing_media: tuple[MissingMedia, ...],
    ) -> tuple[ImportRun, int, bool]:
        async with session_factory() as session, session.begin():
            run = await session.scalar(
                select(ImportRun).where(ImportRun.source_fingerprint == fingerprint)
            )
            if run is None:
                stats = _initial_stats(plan, missing_media)
                run = ImportRun(
                    kind="telegram_desktop_json",
                    source_fingerprint=fingerprint,
                    status="running",
                    started_at=utc_now(),
                    stats=stats,
                )
                session.add(run)
                await session.flush()
                return run, 0, False

            stats = _normalized_stats(run.stats, plan)
            if int(stats["normalized_chat_id"]) != self._target_chat_id:
                raise RuntimeError("stored import run belongs to another target chat")
            resumed_from = min(int(stats["next_index"]), len(plan.messages))
            if run.status == "succeeded":
                return run, resumed_from, True

            run.status = "running"
            run.started_at = run.started_at or utc_now()
            run.completed_at = None
            run.error = None
            run.stats = stats
            return run, resumed_from, False

    async def _persist_message(
        self,
        session: AsyncSession,
        imported: ImportedMessage,
        *,
        export_root: Path,
    ) -> _PersistResult:
        """Upsert one message without committing the caller's batch transaction."""

        user, user_created = await _upsert_user(session, imported)
        topic, topic_created = await _upsert_topic(session, imported)
        media_group, media_group_created = await _upsert_media_group(session, imported)
        stored = await session.scalar(
            select(Message).where(
                Message.chat_id == imported.chat_id,
                Message.message_id == imported.message_id,
            )
        )
        message_created = stored is None
        if stored is None:
            stored = Message(
                chat_id=imported.chat_id,
                message_id=imported.message_id,
                sent_at=imported.sent_at,
                source="telegram_export",
            )
            session.add(stored)

        _merge_message(
            stored,
            imported,
            user=user,
            topic=topic,
            media_group=media_group,
            created=message_created,
        )
        await session.flush()
        file_created = await _upsert_local_file(
            session,
            stored,
            imported,
            export_root=export_root,
        )
        await session.flush()
        await upsert_message_search_document(
            session,
            stored,
            telegram_link=_telegram_link(stored.chat_id, stored.message_id),
        )
        return _PersistResult(
            message_created=message_created,
            user_created=user_created,
            topic_created=topic_created,
            media_group_created=media_group_created,
            file_created=file_created,
        )


async def _upsert_user(
    session: AsyncSession,
    imported: ImportedMessage,
) -> tuple[User | None, bool]:
    if imported.sender_id is None:
        return None, False
    user = await session.scalar(select(User).where(User.telegram_user_id == imported.sender_id))
    if user is None:
        user = User(
            telegram_user_id=imported.sender_id,
            display_name=imported.sender_display_name,
            is_bot=False,
            first_seen_at=imported.sent_at,
            last_seen_at=imported.sent_at,
        )
        session.add(user)
        await session.flush()
        return user, True
    user.first_seen_at = min(user.first_seen_at, imported.sent_at)
    user.last_seen_at = max(user.last_seen_at, imported.sent_at)
    if imported.sender_display_name and not user.display_name:
        user.display_name = imported.sender_display_name
    return user, False


async def _upsert_topic(
    session: AsyncSession,
    imported: ImportedMessage,
) -> tuple[Topic | None, bool]:
    if imported.topic_id is None:
        return None, False
    topic = await session.scalar(
        select(Topic).where(
            Topic.chat_id == imported.chat_id,
            Topic.thread_id == imported.topic_id,
        )
    )
    if topic is None:
        topic = Topic(
            chat_id=imported.chat_id,
            thread_id=imported.topic_id,
            name="General" if imported.topic_id == 1 else f"Topic {imported.topic_id}",
            kind="forum",
            created_at=imported.sent_at,
            updated_at=utc_now(),
        )
        session.add(topic)
        await session.flush()
        return topic, True
    topic.created_at = min(topic.created_at, imported.sent_at)
    return topic, False


async def _upsert_media_group(
    session: AsyncSession,
    imported: ImportedMessage,
) -> tuple[MediaGroup | None, bool]:
    if imported.media_group_id is None:
        return None, False
    group = await session.scalar(
        select(MediaGroup).where(
            MediaGroup.chat_id == imported.chat_id,
            MediaGroup.telegram_media_group_id == imported.media_group_id,
        )
    )
    if group is None:
        group = MediaGroup(
            chat_id=imported.chat_id,
            telegram_media_group_id=imported.media_group_id,
            first_message_at=imported.sent_at,
        )
        session.add(group)
        await session.flush()
        return group, True
    group.first_message_at = min(group.first_message_at, imported.sent_at)
    return group, False


def _merge_message(
    stored: Message,
    imported: ImportedMessage,
    *,
    user: User | None,
    topic: Topic | None,
    media_group: MediaGroup | None,
    created: bool,
) -> None:
    # A historical export must not downgrade a newer live Bot API snapshot or erase
    # reusable Bot API metadata.  It may fill facts which were previously absent.
    replace = created or stored.source == "telegram_export"
    if replace or stored.topic_id is None:
        stored.topic_id = topic.id if topic is not None else stored.topic_id
    if replace:
        stored.sender_user_id = user.id if user is not None else None
    elif stored.sender_user_id is None and user is not None:
        stored.sender_user_id = user.id
    if replace or stored.media_group_id is None:
        stored.media_group_id = media_group.id if media_group is not None else stored.media_group_id
    if replace or stored.reply_to_message_id is None:
        stored.reply_to_message_id = imported.reply_to_message_id
    if replace:
        stored.source = "telegram_export"
        stored.message_type = imported.message_type
        stored.sender_display_name = imported.sender_display_name
        stored.forward_metadata = (
            {
                "origin_type": "telegram_export",
                "display_label": imported.forwarded_from,
            }
            if imported.forwarded_from
            else None
        )
        if imported.file_path is not None:
            stored.text = None
            stored.caption = imported.text or None
        else:
            stored.text = imported.text or None
            stored.caption = None
        # Entity offsets are unavailable after Telegram Desktop text flattening.
        stored.entities = None
        stored.sent_at = imported.sent_at
        stored.edited_at = imported.edited_at
    else:
        if stored.sender_display_name is None:
            stored.sender_display_name = imported.sender_display_name
        if stored.text is None and stored.caption is None and imported.text:
            if imported.file_path is not None:
                stored.caption = imported.text
            else:
                stored.text = imported.text
        if stored.forward_metadata is None and imported.forwarded_from:
            stored.forward_metadata = {
                "origin_type": "telegram_export",
                "display_label": imported.forwarded_from,
            }
        if stored.edited_at is None:
            stored.edited_at = imported.edited_at
    stored.ingested_at = utc_now()


async def _upsert_local_file(
    session: AsyncSession,
    stored_message: Message,
    imported: ImportedMessage,
    *,
    export_root: Path,
) -> bool:
    if imported.file_path is None:
        return False
    relative_path = _relative_export_path(imported.file_path, export_root)
    opaque_id = _local_file_identifier(imported, relative_path)
    stored = await session.scalar(
        select(File).where(
            File.message_id == stored_message.id,
            File.telegram_file_unique_id == opaque_id,
        )
    )
    created = stored is None
    if stored is None:
        stored = File(
            message_id=stored_message.id,
            # This explicit namespace is deliberately not a reusable Bot API file_id.
            telegram_file_id=opaque_id,
            telegram_file_unique_id=opaque_id,
            kind=_file_kind(imported.message_type),
        )
        session.add(stored)
    stored.kind = _file_kind(imported.message_type)
    stored.mime_type = imported.mime_type
    stored.file_name = imported.filename
    stored.size_bytes = imported.file_size
    stored.storage_path = _STORAGE_PATH_PREFIX + quote(relative_path, safe="/._-")
    # The importer does not read file content, so it must not claim a content hash.
    stored.sha256 = None
    return created


def _media_state(
    imported: ImportedMessage,
    *,
    export_root: Path | None,
) -> MissingMedia | None:
    path = imported.file_path
    if path is None:
        return None
    if export_root is not None:
        relative_path = _relative_export_path(path, export_root)
    else:
        # ``ImportedMessage.file_path`` is already a parser-validated canonical path.
        relative_path = path.name
    try:
        mode = path.stat(follow_symlinks=False).st_mode
    except FileNotFoundError:
        return MissingMedia(imported.message_id, relative_path, "missing")
    except OSError:
        return MissingMedia(imported.message_id, relative_path, "unreadable")
    if not stat.S_ISREG(mode):
        return MissingMedia(imported.message_id, relative_path, "not_a_regular_file")
    return None


def inspect_telegram_desktop_export(export_path: Path) -> TelegramDesktopImportPreflight:
    """Inspect an exact export without opening a database or mutating source files."""

    resolved = export_path.expanduser().resolve(strict=True)
    fingerprint = telegram_export_fingerprint(resolved)
    data = load_telegram_desktop_export(resolved)
    plan = plan_telegram_desktop_import(data, export_root=resolved.parent)
    states = tuple(_media_state(message, export_root=resolved.parent) for message in plan.messages)
    return TelegramDesktopImportPreflight(
        resolved_path=resolved,
        source_fingerprint=fingerprint,
        plan=plan,
        missing_media=tuple(item for item in states if item is not None),
    )


async def reconcile_telegram_desktop_import(
    session_factory: async_sessionmaker[AsyncSession],
    prepared: TelegramDesktopImportPreflight,
) -> ImportReconciliation:
    """Verify all planned message, file, search, and in-export reply projections."""

    planned = {message.message_id: message for message in prepared.plan.messages}
    planned_ids = tuple(planned)
    archived: dict[int, tuple[int, int | None]] = {}
    async with session_factory() as session:
        for chunk in _chunks(planned_ids, 400):
            rows = await session.execute(
                select(Message.message_id, Message.id, Message.reply_to_message_id).where(
                    Message.chat_id == prepared.plan.report.normalized_chat_id,
                    Message.message_id.in_(chunk),
                )
            )
            archived.update(
                {
                    message_id: (stored_id, reply_to_message_id)
                    for message_id, stored_id, reply_to_message_id in rows
                }
            )

        internal_ids = tuple(stored_id for stored_id, _reply in archived.values())
        indexed_ids: set[int] = set()
        file_message_ids: set[int] = set()
        for chunk in _chunks(internal_ids, 400):
            indexed_ids.update(
                await session.scalars(
                    select(SearchDocument.source_id).where(
                        SearchDocument.source_type == "message",
                        SearchDocument.source_id.in_(chunk),
                    )
                )
            )
            file_message_ids.update(
                await session.scalars(select(File.message_id).where(File.message_id.in_(chunk)))
            )

    expected_file_message_ids = {
        archived[message_id][0]
        for message_id, message in planned.items()
        if message.file_path is not None and message_id in archived
    }
    reply_links = tuple(
        (message_id, message.reply_to_message_id)
        for message_id, message in planned.items()
        if message.reply_to_message_id is not None and message_id in archived
    )
    reply_mismatches = sum(
        archived[message_id][1] != expected_reply
        for message_id, expected_reply in reply_links
    )
    return ImportReconciliation(
        messages_planned=len(planned),
        messages_archived=len(archived),
        search_documents_verified=len(indexed_ids),
        files_expected=sum(message.file_path is not None for message in planned.values()),
        files_archived=len(expected_file_message_ids & file_message_ids),
        reply_links_checked=len(reply_links),
        reply_link_mismatches=reply_mismatches,
    )


def _chunks(values: tuple[int, ...], size: int) -> Iterator[tuple[int, ...]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _relative_export_path(path: Path, export_root: Path) -> str:
    root = export_root.resolve()
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        # Defensive revalidation in case a caller constructs ImportedMessage directly.
        raise ValueError("media path escapes the export directory")
    relative = resolved.relative_to(root)
    if not relative.parts:
        raise ValueError("media path must name a file inside the export directory")
    return relative.as_posix()


def _local_file_identifier(imported: ImportedMessage, relative_path: str) -> str:
    payload = f"{imported.chat_id}\0{imported.message_id}\0{relative_path}".encode()
    return _LOCAL_FILE_ID_PREFIX + hashlib.sha256(payload).hexdigest()


def _file_kind(message_type: str) -> str:
    aliases = {"file": "document", "video_file": "video"}
    return aliases.get(message_type, message_type)[:24]


def _telegram_link(chat_id: int, message_id: int) -> str | None:
    rendered = str(chat_id)
    if not rendered.startswith("-100"):
        return None
    return f"https://t.me/c/{rendered[4:]}/{message_id}"


def _initial_stats(
    plan: ImportPlan,
    missing_media: tuple[MissingMedia, ...],
) -> dict[str, Any]:
    return {
        "version": _STATS_VERSION,
        "raw_chat_id": plan.report.raw_chat_id,
        "chat_type": plan.report.chat_type,
        "normalized_chat_id": plan.report.normalized_chat_id,
        "messages_found": plan.report.messages_found,
        "messages_planned": len(plan.messages),
        "parser_failure_count": len(plan.report.failures),
        "missing_media_count": len(missing_media),
        "missing_media_sample": [
            {
                "message_id": item.message_id,
                "relative_path": item.relative_path,
                "reason": item.reason,
            }
            for item in missing_media[:_MAX_REPORTED_MEDIA]
        ],
        "next_index": 0,
        "batches_completed": 0,
        "messages_created": 0,
        "messages_updated": 0,
        "users_created": 0,
        "topics_created": 0,
        "media_groups_created": 0,
        "files_created": 0,
    }


def _normalized_stats(stats: Mapping[str, Any] | None, plan: ImportPlan) -> dict[str, Any]:
    defaults = _initial_stats(plan, ())
    if stats is None:
        return defaults
    normalized = {**defaults, **dict(stats)}
    if int(normalized.get("version", -1)) != _STATS_VERSION:
        raise RuntimeError("unsupported Telegram export import-run stats version")
    if (
        int(normalized["raw_chat_id"]) != plan.report.raw_chat_id
        or str(normalized["chat_type"]) != plan.report.chat_type
        or int(normalized["normalized_chat_id"]) != plan.report.normalized_chat_id
    ):
        raise RuntimeError("stored import run belongs to another Telegram export chat")
    if int(normalized["messages_planned"]) != len(plan.messages):
        raise RuntimeError("stored import run does not match the parsed source")
    return normalized


def _record_result(stats: dict[str, Any], result: _PersistResult) -> None:
    key = "messages_created" if result.message_created else "messages_updated"
    stats[key] = int(stats[key]) + 1
    for counter, created in (
        ("users_created", result.user_created),
        ("topics_created", result.topic_created),
        ("media_groups_created", result.media_group_created),
        ("files_created", result.file_created),
    ):
        if created:
            stats[counter] = int(stats[counter]) + 1


async def _mark_failed(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: int,
    exc: Exception,
) -> None:
    async with session_factory() as session, session.begin():
        run = await session.get(ImportRun, run_id)
        if run is not None:
            run.status = "failed"
            # Persist the class, not a potentially secret-bearing exception string.
            run.error = f"{type(exc).__name__}: import batch failed"


def _build_report(
    run: ImportRun,
    *,
    fingerprint: str,
    plan: ImportPlan,
    missing_media: tuple[MissingMedia, ...],
    resumed_from: int,
    already_completed: bool,
) -> PersistentImportReport:
    stats = _normalized_stats(run.stats, plan)
    return PersistentImportReport(
        import_run_id=run.id,
        source_fingerprint=fingerprint,
        status=run.status,
        raw_chat_id=plan.report.raw_chat_id,
        chat_type=plan.report.chat_type,
        normalized_chat_id=plan.report.normalized_chat_id,
        messages_found=plan.report.messages_found,
        messages_planned=len(plan.messages),
        messages_processed=int(stats["next_index"]),
        messages_created=int(stats["messages_created"]),
        messages_updated=int(stats["messages_updated"]),
        users_created=int(stats["users_created"]),
        topics_created=int(stats["topics_created"]),
        media_groups_created=int(stats["media_groups_created"]),
        files_created=int(stats["files_created"]),
        parser_failures=plan.report.failures,
        missing_media=missing_media,
        limitations=plan.report.limitations,
        resumed_from=resumed_from,
        already_completed=already_completed,
    )
