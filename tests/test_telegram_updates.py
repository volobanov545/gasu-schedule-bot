from __future__ import annotations

from datetime import UTC, datetime

from aiogram.types import MessageReactionUpdated

from szs_hub.telegram.updates import ALLOWED_UPDATES, attendance_event_from_update


def reaction_update(
    *,
    old: list[dict[str, str]],
    new: list[dict[str, str]],
    include_user: bool = True,
) -> MessageReactionUpdated:
    data: dict[str, object] = {
        "chat": {"id": -1001, "type": "supergroup", "title": "Test"},
        "message_id": 42,
        "date": datetime(2026, 9, 1, 9, 5, tzinfo=UTC),
        "old_reaction": old,
        "new_reaction": new,
    }
    if include_user:
        data["user"] = {"id": 777, "is_bot": False, "first_name": "Test"}
    else:
        data["actor_chat"] = {"id": -1001, "type": "supergroup", "title": "Test"}
    return MessageReactionUpdated.model_validate(data)


def test_expected_emoji_add_and_remove_are_reduced() -> None:
    added = attendance_event_from_update(
        reaction_update(old=[], new=[{"type": "emoji", "emoji": "❤"}]),
        expected_reaction="❤",
    )
    removed = attendance_event_from_update(
        reaction_update(old=[{"type": "emoji", "emoji": "❤"}], new=[]),
        expected_reaction="❤",
    )

    assert added is not None and added.is_present is True and added.user_id == 777
    assert removed is not None and removed.is_present is False


def test_other_reaction_change_has_no_attendance_event() -> None:
    update = reaction_update(old=[], new=[{"type": "emoji", "emoji": "😂"}])

    assert attendance_event_from_update(update, expected_reaction="❤") is None


def test_actor_chat_is_not_a_personal_mark() -> None:
    update = reaction_update(
        old=[],
        new=[{"type": "emoji", "emoji": "❤"}],
        include_user=False,
    )

    assert attendance_event_from_update(update, expected_reaction="❤") is None


def test_sensitive_updates_are_explicitly_allowed() -> None:
    assert "message_reaction" in ALLOWED_UPDATES
    assert "chat_member" in ALLOWED_UPDATES
    assert len(ALLOWED_UPDATES) == len(set(ALLOWED_UPDATES))

