"""Tests for LinkGroup domain model."""

from __future__ import annotations

import re

import pytest

from careerkit.jobs.domain.link_model import (
    LinkGroup,
    LinkSchemaError,
    generate_group_id,
)
from careerkit.jobs.domain.model import JobKey


def test_generate_group_id_format():
    gid = generate_group_id()
    assert re.fullmatch(r"[0-9a-f]{32}", gid)


def test_create_link_group():
    k1 = JobKey("saramin", "111")
    k2 = JobKey("wanted", "222")
    g = LinkGroup(
        group_id="0123456789abcdef0123456789abcdef",
        members=(k1, k2),
        created_at="2026-08-25T01:00:00+09:00",
    )
    assert len(g.group_id) == 32
    assert len(g.members) == 2
    assert g.note is None
    assert g.schema_version == 1


def test_roundtrip_serialization():
    k1 = JobKey("saramin", "111")
    k2 = JobKey("wanted", "222")
    original = LinkGroup(
        group_id="0123456789abcdef0123456789abcdef",
        members=(k1, k2),
        created_at="2026-08-25T01:00:00+09:00",
        note="same company",
    )
    d = original.to_dict()
    restored = LinkGroup.from_dict(d)
    assert restored == original


def test_reject_single_member():
    with pytest.raises(ValueError, match="최소 2개"):
        LinkGroup(
            group_id="0123456789abcdef0123456789abcdef",
            members=(JobKey("wanted", "111"),),
            created_at="2026-08-25T01:00:00+09:00",
        )


def test_reject_duplicate_members():
    k = JobKey("wanted", "111")
    with pytest.raises(ValueError, match="중복"):
        LinkGroup(
            group_id="0123456789abcdef0123456789abcdef",
            members=(k, k),
            created_at="2026-08-25T01:00:00+09:00",
        )


def test_members_sorted():
    k1 = JobKey("wanted", "222")
    k2 = JobKey("saramin", "111")
    g = LinkGroup(
        group_id="0123456789abcdef0123456789abcdef",
        members=(k1, k2),
        created_at="2026-08-25T01:00:00+09:00",
    )
    assert g.members[0] == k2
    assert g.members[1] == k1


def test_reject_empty_group_id():
    with pytest.raises(ValueError):
        LinkGroup(
            group_id="",
            members=(JobKey("a", "1"), JobKey("b", "2")),
            created_at="2026-08-25T01:00:00+09:00",
        )


def test_three_members_allowed():
    g = LinkGroup(
        group_id="0123456789abcdef0123456789abcdef",
        members=(
            JobKey("wanted", "1"),
            JobKey("saramin", "2"),
            JobKey("remember", "3"),
        ),
        created_at="2026-08-25T01:00:00+09:00",
    )
    assert len(g.members) == 3



@pytest.mark.parametrize(
    "bad_group_id",
    [
        "../../evil",
        "abc",
        "0123456789abcdef0123456789ABCDEF",  # uppercase
        "0123456789abcdef0123456789abcdefg",  # not hex
        "0123456789abcdef_123456789abcdef",  # non-hex char
        "0123456789abcdef0123456789abcde",  # 31 chars
        "0123456789abcdef0123456789abcdef0",  # 33 chars
    ],
)
def test_reject_invalid_group_id(bad_group_id):
    with pytest.raises(ValueError):
        LinkGroup(
            group_id=bad_group_id,
            members=(JobKey("a", "1"), JobKey("b", "2")),
            created_at="2026-08-25T01:00:00+09:00",
        )


def test_from_dict_rejects_unknown_schema_version():
    g = LinkGroup(
        group_id="0123456789abcdef0123456789abcdef",
        members=(JobKey("a", "1"), JobKey("b", "2")),
        created_at="2026-08-25T01:00:00+09:00",
    )
    d = g.to_dict()
    d["schema_version"] = 2
    with pytest.raises(LinkSchemaError):
        LinkGroup.from_dict(d)
