"""Narrow, testable adapters from aiogram models to domain events."""

from __future__ import annotations

from aiogram.types import MessageReactionUpdated, ReactionTypeEmoji

from szs_hub.domain.attendance import ReactionEvent

ALLOWED_UPDATES: tuple[str, ...] = (
    "message",
    "edited_message",
    "message_reaction",
    "message_reaction_count",
    "chat_member",
    "my_chat_member",
    "callback_query",
)


def attendance_event_from_update(
    update: MessageReactionUpdated,
    *,
    expected_reaction: str,
) -> ReactionEvent | None:
    """Ignore anonymous actors and reduce full old/new sets to one attendance toggle."""

    if update.user is None or update.user.is_bot:
        return None
    old_present = _contains_emoji(update.old_reaction, expected_reaction)
    new_present = _contains_emoji(update.new_reaction, expected_reaction)
    if old_present == new_present:
        return None
    return ReactionEvent(
        user_id=update.user.id,
        occurred_at=update.date,
        reaction=expected_reaction,
        is_present=new_present,
    )


def _contains_emoji(reactions: object, expected: str) -> bool:
    if not isinstance(reactions, list):
        return False
    return any(
        isinstance(reaction, ReactionTypeEmoji) and reaction.emoji == expected
        for reaction in reactions
    )

