"""Tests for LinkGroup domain model."""

from __future__ import annotations

import re

import pytest

from careerkit.jobs.domain.link_model import LinkGroup, generate_group_id
from careerkit.jobs.domain.model import JobKey


def test_generate_group_id_format():
    gid = generate_group_id()
    assert re.fullmatch(r"[0-9a-f]{8}", gid)


def test_create_link_group():
    k1 = JobKey("saramin", "111")
    k2 = JobKey("wanted", "222")
    g = LinkGroup(
        group_id="abcd1234",
        members=(k1, k2),
        created_at="2026-08-25T01:00:00+09:00",
    )
    assert len(g.group_id) == 8
    assert len(g.members) == 2
    assert g.note is None
    assert g.schema_version == 1


def test_roundtrip_serialization():
    k1 = JobKey("saramin", "111")
    k2 = JobKey("wanted", "222")
    original = LinkGroup(
        group_id="abcd1234",
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
            group_id="abcd1234",
            members=(JobKey("wanted", "111"),),
            created_at="2026-08-25T01:00:00+09:00",
        )


def test_reject_duplicate_members():
    k = JobKey("wanted", "111")
    with pytest.raises(ValueError, match="중복"):
        LinkGroup(
            group_id="abcd1234",
            members=(k, k),
            created_at="2026-08-25T01:00:00+09:00",
        )


def test_members_sorted():
    k1 = JobKey("wanted", "222")
    k2 = JobKey("saramin", "111")
    g = LinkGroup(
        group_id="abcd1234",
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
        group_id="abcd1234",
        members=(
            JobKey("wanted", "1"),
            JobKey("saramin", "2"),
            JobKey("remember", "3"),
        ),
        created_at="2026-08-25T01:00:00+09:00",
    )
    assert len(g.members) == 3
