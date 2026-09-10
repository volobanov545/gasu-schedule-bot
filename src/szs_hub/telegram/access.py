"""Fail-closed member authorization for access to private group history."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class MemberStatus(StrEnum):
    CREATOR = "creator"
    ADMINISTRATOR = "administrator"
    MEMBER = "member"
    RESTRICTED = "restricted"
    LEFT = "left"
    KICKED = "kicked"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Membership:
    status: MemberStatus
    is_member: bool | None = None


def can_access_group_history(membership: Membership | None) -> bool:
    """Allow only a currently confirmed group member; errors and unknowns deny."""

    if membership is None:
        return False
    if membership.status in {
        MemberStatus.CREATOR,
        MemberStatus.ADMINISTRATOR,
        MemberStatus.MEMBER,
    }:
        return True
    return membership.status is MemberStatus.RESTRICTED and membership.is_member is True

