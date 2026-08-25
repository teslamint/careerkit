"""Domain model for cross-platform JD link groups."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping
from uuid import uuid4

from careerkit.jobs.domain.model import JobKey


LINK_SCHEMA_VERSION = 1

_GROUP_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def generate_group_id() -> str:
    return uuid4().hex


def is_valid_group_id(group_id: str) -> bool:
    return bool(_GROUP_ID_PATTERN.match(group_id))


class LinkSchemaError(Exception):
    """Raised when a persisted group file carries an unsupported schema version."""


@dataclass(frozen=True)
class LinkGroup:
    group_id: str
    members: tuple[JobKey, ...]
    created_at: str
    note: str | None = None
    schema_version: int = LINK_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not is_valid_group_id(self.group_id):
            raise ValueError("group_id must be 32 lowercase hex characters")
        if len(self.members) < 2:
            raise ValueError("최소 2개 멤버 필요")
        seen: set[tuple[str, str]] = set()
        for m in self.members:
            key = (m.platform, m.job_id)
            if key in seen:
                raise ValueError(f"중복 멤버: {m.platform}:{m.job_id}")
            seen.add(key)
        sorted_members = tuple(sorted(self.members))
        object.__setattr__(self, "members", sorted_members)

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "members": [
                {"platform": m.platform, "job_id": m.job_id} for m in self.members
            ],
            "created_at": self.created_at,
            "note": self.note,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> LinkGroup:
        schema_version = data.get("schema_version", LINK_SCHEMA_VERSION)
        if schema_version not in {LINK_SCHEMA_VERSION}:
            raise LinkSchemaError(f"지원하지 않는 schema_version: {schema_version}")
        members = tuple(
            JobKey(m["platform"], m["job_id"]) for m in data["members"]
        )
        return cls(
            group_id=str(data["group_id"]),
            members=members,
            created_at=str(data["created_at"]),
            note=data.get("note"),
            schema_version=schema_version,
        )
