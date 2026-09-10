from __future__ import annotations

import pytest

from szs_hub.telegram.access import (
    Membership,
    MemberStatus,
    can_access_group_history,
)


@pytest.mark.parametrize(
    "status",
    [MemberStatus.CREATOR, MemberStatus.ADMINISTRATOR, MemberStatus.MEMBER],
)
def test_confirmed_members_are_allowed(status: MemberStatus) -> None:
    assert can_access_group_history(Membership(status)) is True


def test_restricted_user_must_still_be_a_member() -> None:
    assert can_access_group_history(Membership(MemberStatus.RESTRICTED, True)) is True
    assert can_access_group_history(Membership(MemberStatus.RESTRICTED, False)) is False
    assert can_access_group_history(Membership(MemberStatus.RESTRICTED, None)) is False


@pytest.mark.parametrize(
    "membership",
    [
        None,
        Membership(MemberStatus.LEFT),
        Membership(MemberStatus.KICKED),
        Membership(MemberStatus.UNKNOWN),
    ],
)
def test_non_members_and_errors_fail_closed(membership: Membership | None) -> None:
    assert can_access_group_history(membership) is False
