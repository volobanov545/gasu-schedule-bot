"""Declarative database model for durable Telegram automation."""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from szs_hub.storage.base import Base, UTCDateTime, utc_now

_CURRENT_TIMESTAMP = text("CURRENT_TIMESTAMP")


class InboxUpdate(Base):
    """Durable raw-update inbox; ``update_id`` makes admission idempotent."""

    __tablename__ = "inbox_updates"
    __table_args__ = (
        CheckConstraint("attempts >= 0", name="attempts_nonnegative"),
        Index("ix_inbox_updates_ready", "processed_at", "available_at", "received_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    update_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, server_default=_CURRENT_TIMESTAMP
    )
    available_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, server_default=_CURRENT_TIMESTAMP
    )
    claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    processed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)


class ProcessedUpdate(Base):
    """Terminal update outcome, separate from the durable inbox payload."""

    __tablename__ = "processed_updates"
    __table_args__ = (
        CheckConstraint("outcome IN ('processed', 'ignored', 'failed')", name="valid_outcome"),
        Index("ix_processed_updates_processed_at", "processed_at"),
    )

    update_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("inbox_updates.update_id", ondelete="CASCADE"),
        primary_key=True,
    )
    handler: Mapped[str] = mapped_column(String(100), nullable=False)
    outcome: Mapped[str] = mapped_column(
        String(16), nullable=False, default="processed", server_default="processed"
    )
    processed_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, server_default=_CURRENT_TIMESTAMP
    )
    detail: Mapped[str | None] = mapped_column(Text)


class User(Base):
    """Telegram user identity; the surrogate key keeps relationships compact."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    username: Mapped[str | None] = mapped_column(String(64))
    display_name: Mapped[str | None] = mapped_column(String(256))
    language_code: Mapped[str | None] = mapped_column(String(16))
    is_bot: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    first_seen_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, server_default=_CURRENT_TIMESTAMP
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, server_default=_CURRENT_TIMESTAMP
    )


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("chat_id", "user_id"),
        CheckConstraint(
            "status IN ('creator', 'administrator', 'member', 'restricted', 'left', 'kicked', "
            "'unknown')",
            name="valid_status",
        ),
        Index("ix_memberships_chat_status", "chat_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="unknown", server_default="unknown"
    )
    is_headman: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    joined_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    left_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    checked_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, server_default=_CURRENT_TIMESTAMP
    )


class Topic(Base):
    __tablename__ = "topics"
    __table_args__ = (
        UniqueConstraint("chat_id", "thread_id"),
        Index("ix_topics_chat_kind", "chat_id", "kind"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    thread_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    kind: Mapped[str | None] = mapped_column(String(32))
    is_closed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, server_default=_CURRENT_TIMESTAMP
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, server_default=_CURRENT_TIMESTAMP
    )


class MediaGroup(Base):
    __tablename__ = "media_groups"
    __table_args__ = (
        UniqueConstraint("chat_id", "telegram_media_group_id"),
        Index("ix_media_groups_chat_received", "chat_id", "first_message_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    telegram_media_group_id: Mapped[str] = mapped_column(String(128), nullable=False)
    first_message_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    finalized_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("chat_id", "message_id"),
        CheckConstraint("source IN ('bot_api', 'telegram_export')", name="valid_source"),
        Index("ix_messages_sent_at", "sent_at"),
        Index("ix_messages_sender_sent", "sender_user_id", "sent_at"),
        Index("ix_messages_topic_sent", "topic_id", "sent_at"),
        Index("ix_messages_media_group", "media_group_id"),
        Index("ix_messages_type_sent", "message_type", "sent_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    topic_id: Mapped[int | None] = mapped_column(ForeignKey("topics.id", ondelete="SET NULL"))
    sender_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    media_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("media_groups.id", ondelete="SET NULL")
    )
    reply_to_message_id: Mapped[int | None] = mapped_column(BigInteger)
    source: Mapped[str] = mapped_column(
        String(24), nullable=False, default="bot_api", server_default="bot_api"
    )
    message_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="unknown", server_default="unknown"
    )
    sender_display_name: Mapped[str | None] = mapped_column(String(256))
    forward_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    text: Mapped[str | None] = mapped_column(Text)
    caption: Mapped[str | None] = mapped_column(Text)
    entities: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    sent_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    edited_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    ingested_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, server_default=_CURRENT_TIMESTAMP
    )


class File(Base):
    __tablename__ = "files"
    __table_args__ = (
        UniqueConstraint("message_id", "telegram_file_unique_id"),
        CheckConstraint("size_bytes IS NULL OR size_bytes >= 0", name="size_nonnegative"),
        CheckConstraint(
            "extraction_status IS NULL OR extraction_status IN ("
            "'ok', 'needs_ocr', 'unsupported', 'type_mismatch', 'limit_exceeded', "
            "'password_protected', 'corrupt', 'timeout', 'error')",
            name="valid_extraction_status",
        ),
        Index("ix_files_unique_id", "telegram_file_unique_id"),
        Index("ix_files_kind", "kind"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    telegram_file_id: Mapped[str] = mapped_column(String(512), nullable=False)
    telegram_file_unique_id: Mapped[str] = mapped_column(String(256), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(255))
    file_name: Mapped[str | None] = mapped_column(String(512))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    sha256: Mapped[str | None] = mapped_column(String(64))
    storage_path: Mapped[str | None] = mapped_column(Text)
    extracted_text: Mapped[str | None] = mapped_column(Text)
    extraction_status: Mapped[str | None] = mapped_column(String(32))
    extraction_error_code: Mapped[str | None] = mapped_column(String(100))
    extraction_version: Mapped[str | None] = mapped_column(String(64))
    extracted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, server_default=_CURRENT_TIMESTAMP
    )


class Material(Base):
    __tablename__ = "materials"
    __table_args__ = (
        UniqueConstraint("source_message_id"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="valid_confidence"),
        CheckConstraint(
            "workflow_state IN ('discovered', 'review', 'copy_pending', 'copied', "
            "'delete_pending', 'completed', 'failed')",
            name="valid_workflow_state",
        ),
        Index("ix_materials_type_created", "material_type", "created_at"),
        Index("ix_materials_discipline", "discipline"),
        Index("ix_materials_workflow", "workflow_state", "updated_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_message_id: Mapped[int] = mapped_column(
        ForeignKey("messages.id", ondelete="RESTRICT"), nullable=False
    )
    source_media_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("media_groups.id", ondelete="SET NULL")
    )
    destination_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    destination_message_id: Mapped[int | None] = mapped_column(BigInteger)
    title: Mapped[str | None] = mapped_column(String(512))
    discipline: Mapped[str | None] = mapped_column(String(256))
    material_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="unknown", server_default="unknown"
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    workflow_state: Mapped[str] = mapped_column(
        String(24), nullable=False, default="discovered", server_default="discovered"
    )
    classifier_version: Mapped[str | None] = mapped_column(String(64))
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, server_default=_CURRENT_TIMESTAMP
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, server_default=_CURRENT_TIMESTAMP
    )


class ScheduleSnapshot(Base):
    __tablename__ = "schedule_snapshots"
    __table_args__ = (
        UniqueConstraint("source", "group_key", "week_start", "content_hash"),
        Index(
            "ix_schedule_snapshots_group_week_fetched",
            "group_key",
            "week_start",
            "fetched_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    group_key: Mapped[str] = mapped_column(String(128), nullable=False)
    week_start: Mapped[date] = mapped_column(Date, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    is_complete: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    raw_payload: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, server_default=_CURRENT_TIMESTAMP
    )


class Lesson(Base):
    __tablename__ = "lessons"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "lesson_key"),
        CheckConstraint("ends_at > starts_at", name="positive_duration"),
        Index("ix_lessons_day_time", "day", "starts_at"),
        Index("ix_lessons_snapshot_day", "snapshot_id", "day"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("schedule_snapshots.id", ondelete="CASCADE"), nullable=False
    )
    lesson_key: Mapped[str] = mapped_column(String(128), nullable=False)
    source_id: Mapped[str | None] = mapped_column(String(256))
    day: Mapped[date] = mapped_column(Date, nullable=False)
    starts_at: Mapped[time] = mapped_column(Time, nullable=False)
    ends_at: Mapped[time] = mapped_column(Time, nullable=False)
    subject: Mapped[str] = mapped_column(String(512), nullable=False)
    lesson_type: Mapped[str | None] = mapped_column(String(128))
    teacher: Mapped[str | None] = mapped_column(String(256))
    room: Mapped[str | None] = mapped_column(String(128))
    building: Mapped[str | None] = mapped_column(String(128))
    subgroup: Mapped[str | None] = mapped_column(String(128))


class ScheduleChange(Base):
    __tablename__ = "schedule_changes"
    __table_args__ = (
        UniqueConstraint("to_snapshot_id", "fingerprint"),
        CheckConstraint(
            "kind IN ('added', 'cancelled', 'time', 'room', 'building', 'teacher', "
            "'lesson_type', 'subject')",
            name="valid_kind",
        ),
        CheckConstraint(
            "before_lesson_id IS NOT NULL OR after_lesson_id IS NOT NULL",
            name="has_lesson",
        ),
        Index("ix_schedule_changes_pending", "notified_at", "detected_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    from_snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("schedule_snapshots.id", ondelete="CASCADE"), nullable=False
    )
    to_snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("schedule_snapshots.id", ondelete="CASCADE"), nullable=False
    )
    before_lesson_id: Mapped[int | None] = mapped_column(
        ForeignKey("lessons.id", ondelete="SET NULL")
    )
    after_lesson_id: Mapped[int | None] = mapped_column(
        ForeignKey("lessons.id", ondelete="SET NULL")
    )
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    detected_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, server_default=_CURRENT_TIMESTAMP
    )
    notified_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class AttendanceSession(Base):
    __tablename__ = "attendance_sessions"
    __table_args__ = (
        UniqueConstraint("chat_id", "message_id"),
        UniqueConstraint("lesson_id"),
        CheckConstraint("closes_at > opens_at", name="positive_window"),
        CheckConstraint(
            "status IN ('scheduled', 'open', 'closed', 'cancelled')", name="valid_status"
        ),
        Index("ix_attendance_sessions_status_window", "status", "opens_at", "closes_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lesson_id: Mapped[int] = mapped_column(
        ForeignKey("lessons.id", ondelete="RESTRICT"), nullable=False
    )
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reaction: Mapped[str] = mapped_column(
        String(32), nullable=False, default="❤", server_default="❤"
    )
    opens_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    closes_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="scheduled", server_default="scheduled"
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, server_default=_CURRENT_TIMESTAMP
    )


class AttendanceMark(Base):
    __tablename__ = "attendance_marks"
    __table_args__ = (
        UniqueConstraint("session_id", "user_id"),
        CheckConstraint("removed_at IS NULL OR removed_at >= marked_at", name="valid_removal_time"),
        Index("ix_attendance_marks_user_marked", "user_id", "marked_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("attendance_sessions.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    source_update_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("inbox_updates.update_id", ondelete="SET NULL")
    )
    marked_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    removed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')",
            name="valid_status",
        ),
        CheckConstraint("attempts >= 0", name="attempts_nonnegative"),
        CheckConstraint("max_attempts > 0", name="max_attempts_positive"),
        Index("ix_jobs_ready", "status", "run_at"),
        Index("ix_jobs_lease", "status", "locked_until"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(100), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    run_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, server_default=_CURRENT_TIMESTAMP
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=5, server_default="5"
    )
    locked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    locked_until: Mapped[datetime | None] = mapped_column(UTCDateTime())
    locked_by: Mapped[str | None] = mapped_column(String(128))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, server_default=_CURRENT_TIMESTAMP
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, server_default=_CURRENT_TIMESTAMP
    )
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class OutboxMessage(Base):
    __tablename__ = "outbox"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'sending', 'sent', 'failed', 'cancelled')",
            name="valid_status",
        ),
        CheckConstraint("attempts >= 0", name="attempts_nonnegative"),
        CheckConstraint("max_attempts > 0", name="max_attempts_positive"),
        Index("ix_outbox_ready", "status", "available_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(100), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    available_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, server_default=_CURRENT_TIMESTAMP
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=5, server_default="5"
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, server_default=_CURRENT_TIMESTAMP
    )
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class AIConversation(Base):
    __tablename__ = "ai_conversations"
    __table_args__ = (Index("ix_ai_conversations_user_updated", "user_id", "updated_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active"
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, server_default=_CURRENT_TIMESTAMP
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, server_default=_CURRENT_TIMESTAMP
    )
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class AIMessage(Base):
    __tablename__ = "ai_messages"
    __table_args__ = (
        CheckConstraint("role IN ('system', 'user', 'assistant', 'tool')", name="valid_role"),
        CheckConstraint("token_count IS NULL OR token_count >= 0", name="token_count_nonnegative"),
        Index("ix_ai_messages_conversation_created", "conversation_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("ai_conversations.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(128))
    token_count: Mapped[int | None] = mapped_column(Integer)
    sources: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, server_default=_CURRENT_TIMESTAMP
    )


class ImportRun(Base):
    __tablename__ = "import_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')",
            name="valid_status",
        ),
        Index("ix_import_runs_status_started", "status", "started_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    source_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    stats: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, server_default=_CURRENT_TIMESTAMP
    )


class SystemSetting(Base):
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict[str, Any] | list[Any] | str | int | float | bool | None] = mapped_column(
        JSON, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, server_default=_CURRENT_TIMESTAMP
    )
    updated_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )


class AdminEvent(Base):
    __tablename__ = "admin_events"
    __table_args__ = (
        Index("ix_admin_events_created_at", "created_at"),
        Index("ix_admin_events_type_created", "event_type", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(64))
    target_id: Mapped[str | None] = mapped_column(String(256))
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, server_default=_CURRENT_TIMESTAMP
    )


class SearchDocument(Base):
    """Portable search source; an SQLite FTS5 projection can be rebuilt from it."""

    __tablename__ = "search_documents"
    __table_args__ = (
        UniqueConstraint("source_type", "source_id"),
        Index("ix_search_documents_updated_at", "updated_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSON)
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, server_default=_CURRENT_TIMESTAMP
    )
