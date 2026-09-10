"""Telegram transport adapters and access policy."""

from szs_hub.telegram.updates import ALLOWED_UPDATES, attendance_event_from_update

__all__ = ["ALLOWED_UPDATES", "attendance_event_from_update"]

