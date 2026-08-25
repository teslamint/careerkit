"""File-backed storage for cross-platform JD link groups."""

from __future__ import annotations

import fcntl
import json
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Sequence

from careerkit.jobs.domain.link_model import (
    LinkGroup,
    generate_group_id,
    is_valid_group_id,
)
from careerkit.jobs.domain.model import JobKey

_LOCK_NAME = ".lock"


class LinkStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def create(
        self, members: Sequence[JobKey], *, note: str | None = None
    ) -> LinkGroup:
        self.root.mkdir(parents=True, exist_ok=True)
        with self._locked(exclusive=True):
            for m in members:
                existing = self._get_by_key_unlocked(m)
                if existing is not None:
                    raise ValueError(
                        f"{m.platform}:{m.job_id}는 이미 그룹 {existing.group_id}에 속해 있습니다"
                    )

            from datetime import datetime, timezone

            group = LinkGroup(
                group_id=generate_group_id(),
                members=tuple(members),
                created_at=datetime.now(timezone.utc).astimezone().isoformat(),
                note=note,
            )
            if (self.root / f"{group.group_id}.json").exists():
                raise ValueError(f"링크 그룹 ID 충돌: {group.group_id}")
            self._save_group(group)
            return group

    def get_by_key(self, key: JobKey) -> LinkGroup | None:
        if not self.root.exists():
            return None
        with self._locked(exclusive=False):
            return self._get_by_key_unlocked(key)

    def get_by_group_id(self, group_id: str) -> LinkGroup | None:
        if not is_valid_group_id(group_id):
            return None
        path = self.root / f"{group_id}.json"
        if not path.exists():
            return None
        return self._load_group(path)

    def remove_member(self, key: JobKey) -> LinkGroup | None:
        if not self.root.exists():
            return None
        with self._locked(exclusive=True):
            group = self._get_by_key_unlocked(key)
            if group is None:
                return None
            remaining = tuple(m for m in group.members if m != key)
            path = self.root / f"{group.group_id}.json"
            if len(remaining) < 2:
                path.unlink(missing_ok=True)
                return None
            updated = LinkGroup(
                group_id=group.group_id,
                members=remaining,
                created_at=group.created_at,
                note=group.note,
                schema_version=group.schema_version,
            )
            self._save_group(updated)
            return updated

    def list_all(self) -> list[LinkGroup]:
        if not self.root.exists():
            return []
        groups = []
        for path in sorted(self.root.glob("*.json")):
            group = self._load_group(path)
            if group is not None:
                groups.append(group)
        return groups

    def _get_by_key_unlocked(self, key: JobKey) -> LinkGroup | None:
        for group in self.list_all():
            if key in group.members:
                return group
        return None

    def _load_group(self, path: Path) -> LinkGroup | None:
        try:
            with path.open("r", encoding="utf-8") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                try:
                    data = json.load(f)
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            return LinkGroup.from_dict(data)
        except (json.JSONDecodeError, KeyError, ValueError, OSError):
            return None

    def _save_group(self, group: LinkGroup) -> None:
        target = self.root / f"{group.group_id}.json"
        payload = json.dumps(
            group.to_dict(), ensure_ascii=False, indent=2, sort_keys=True
        ) + "\n"
        fd = tempfile.NamedTemporaryFile(
            dir=self.root, mode="w", encoding="utf-8", suffix=".tmp", delete=False
        )
        tmp_path = Path(fd.name)
        try:
            fd.write(payload)
            fd.flush()
            fd.close()
            tmp_path.replace(target)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

    @contextmanager
    def _locked(self, *, exclusive: bool) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        lock_path = self.root / _LOCK_NAME
        with lock_path.open("a+b") as handle:
            fcntl.flock(
                handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            )
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
