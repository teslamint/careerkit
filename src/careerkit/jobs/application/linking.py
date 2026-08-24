"""LinkService: CRUD and sync for cross-platform JD link groups."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from careerkit.jobs.adapters.storage.file_records import (
    JDRecordRepository,
    JobRecordNotFound,
)
from careerkit.jobs.adapters.storage.link_store import LinkStore
from careerkit.jobs.domain.link_model import LinkGroup
from careerkit.jobs.domain.model import ApplicationStatus, JobKey, PostingStatus


_APP_STATUS_ORDER: dict[ApplicationStatus, int] = {
    ApplicationStatus.PENDING: 0,
    ApplicationStatus.APPLIED: 1,
    ApplicationStatus.INTERVIEW: 2,
    ApplicationStatus.OFFER: 3,
}


@dataclass(frozen=True)
class LinkResult:
    created: bool
    group_id: str | None
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "created": self.created,
            "group_id": self.group_id,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class RemoveResult:
    removed_key: str
    group_deleted: bool
    remaining_members: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "removed_key": self.removed_key,
            "group_deleted": self.group_deleted,
            "remaining_members": self.remaining_members,
        }


@dataclass(frozen=True)
class MemberDetail:
    platform: str
    job_id: str
    company: str | None
    position: str | None
    application_status: str | None
    posting_status: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": f"{self.platform}:{self.job_id}",
            "company": self.company,
            "position": self.position,
            "application_status": self.application_status,
            "posting_status": self.posting_status,
        }


@dataclass(frozen=True)
class GroupDetail:
    group_id: str
    members: tuple[MemberDetail, ...]
    note: str | None
    inconsistent: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "members": [m.to_dict() for m in self.members],
            "note": self.note,
            "inconsistent": self.inconsistent,
        }


@dataclass(frozen=True)
class GroupSummary:
    group_id: str
    member_count: int
    member_keys: tuple[str, ...]
    inconsistent: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "member_count": self.member_count,
            "member_keys": list(self.member_keys),
            "inconsistent": self.inconsistent,
        }


@dataclass(frozen=True)
class SyncChange:
    key: JobKey
    from_status: ApplicationStatus | None
    to_status: ApplicationStatus | None
    posting_status_change: PostingStatus | None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "key": f"{self.key.platform}:{self.key.job_id}",
        }
        if self.to_status is not None:
            result["from_application_status"] = (
                self.from_status.value if self.from_status else None
            )
            result["to_application_status"] = self.to_status.value
        if self.posting_status_change is not None:
            result["posting_status"] = self.posting_status_change.value
        return result


@dataclass(frozen=True)
class SyncResult:
    group_id: str
    changes: tuple[SyncChange, ...]
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "changes": [c.to_dict() for c in self.changes],
            "warnings": list(self.warnings),
        }


class LinkService:
    def __init__(
        self,
        *,
        link_store: LinkStore,
        record_repo: JDRecordRepository,
    ) -> None:
        self._store = link_store
        self._repo = record_repo

    def add_link(self, keys: Sequence[JobKey], *, note: str | None = None) -> LinkResult:
        existing_groups: set[str] = set()
        for k in keys:
            g = self._store.get_by_key(k)
            if g is not None:
                existing_groups.add(g.group_id)

        if len(existing_groups) == 1:
            gid = next(iter(existing_groups))
            group = self._store.get_by_group_id(gid)
            if group is not None and set(group.members) == set(keys):
                return LinkResult(created=False, group_id=gid, warnings=())

        warnings: list[str] = []
        companies: set[str] = set()
        for k in keys:
            try:
                stored = self._repo.get(k)
                companies.add(stored.record.company)
            except JobRecordNotFound:
                warnings.append(f"레코드 없음: {k.platform}:{k.job_id}")
        if len(companies) > 1:
            warnings.append(f"회사명 불일치: {', '.join(sorted(companies))}")

        group = self._store.create(keys, note=note)
        return LinkResult(
            created=True,
            group_id=group.group_id,
            warnings=tuple(warnings),
        )

    def remove_link(self, key: JobKey) -> RemoveResult | None:
        group = self._store.get_by_key(key)
        if group is None:
            return None
        updated = self._store.remove_member(key)
        return RemoveResult(
            removed_key=f"{key.platform}:{key.job_id}",
            group_deleted=updated is None,
            remaining_members=len(updated.members) if updated else 0,
        )

    def show_link(self, key: JobKey) -> GroupDetail | None:
        group = self._store.get_by_key(key)
        if group is None:
            return None
        return self._build_detail(group)

    def list_links(self, *, inconsistent_only: bool = False) -> list[GroupSummary]:
        groups = self._store.list_all()
        summaries = []
        for group in groups:
            inconsistent = self._is_inconsistent(group)
            if inconsistent_only and not inconsistent:
                continue
            summaries.append(
                GroupSummary(
                    group_id=group.group_id,
                    member_count=len(group.members),
                    member_keys=tuple(
                        f"{m.platform}:{m.job_id}" for m in group.members
                    ),
                    inconsistent=inconsistent,
                )
            )
        return summaries

    def check_membership(self, key: JobKey) -> LinkGroup | None:
        return self._store.get_by_key(key)

    def sync(self, key: JobKey, *, dry_run: bool = False) -> SyncResult:
        group = self._store.get_by_key(key)
        if group is None:
            raise ValueError(f"소속된 링크 그룹 없음: {key.platform}:{key.job_id}")

        warnings: list[str] = []
        member_records: dict[JobKey, Any] = {}
        for m in group.members:
            try:
                stored = self._repo.get(m)
                member_records[m] = stored.record
            except JobRecordNotFound:
                warnings.append(f"레코드 없음: {m.platform}:{m.job_id}")

        non_rejected = {
            k: r
            for k, r in member_records.items()
            if r.application_status != ApplicationStatus.REJECTED
        }

        changes: list[SyncChange] = []

        if non_rejected:
            max_order = -1
            for r in non_rejected.values():
                order = _APP_STATUS_ORDER.get(r.application_status, -1)
                if order > max_order:
                    max_order = order

            if max_order >= 0:
                target_status = None
                for status, order in _APP_STATUS_ORDER.items():
                    if order == max_order:
                        target_status = status
                        break

                if target_status is not None:
                    for k, r in non_rejected.items():
                        current_order = _APP_STATUS_ORDER.get(
                            r.application_status, -1
                        )
                        if current_order < max_order:
                            changes.append(
                                SyncChange(
                                    key=k,
                                    from_status=r.application_status,
                                    to_status=target_status,
                                    posting_status_change=None,
                                )
                            )

        has_closed = any(
            r.posting_status == PostingStatus.CLOSED
            for r in member_records.values()
        )
        if has_closed:
            for k, r in member_records.items():
                if r.posting_status != PostingStatus.CLOSED:
                    existing = next(
                        (c for c in changes if c.key == k), None
                    )
                    if existing is not None:
                        idx = changes.index(existing)
                        changes[idx] = SyncChange(
                            key=existing.key,
                            from_status=existing.from_status,
                            to_status=existing.to_status,
                            posting_status_change=PostingStatus.CLOSED,
                        )
                    else:
                        changes.append(
                            SyncChange(
                                key=k,
                                from_status=None,
                                to_status=None,
                                posting_status_change=PostingStatus.CLOSED,
                            )
                        )

        if not dry_run:
            for change in changes:
                source_key = next(
                    (
                        mk
                        for mk in member_records
                        if mk != change.key
                        and _APP_STATUS_ORDER.get(
                            member_records[mk].application_status, -1
                        )
                        == max_order
                    ),
                    None,
                ) if change.to_status is not None else None

                note = None
                if source_key is not None:
                    note = f"링크 그룹 동기화 ({source_key.platform}:{source_key.job_id}에서 전파)"

                self._repo.update_status(
                    change.key,
                    application_status=change.to_status,
                    posting_status=change.posting_status_change,
                    application_note=note,
                )

        return SyncResult(
            group_id=group.group_id,
            changes=tuple(changes),
            warnings=tuple(warnings),
        )

    def _build_detail(self, group: LinkGroup) -> GroupDetail:
        members: list[MemberDetail] = []
        statuses: set[str] = set()
        for m in group.members:
            try:
                stored = self._repo.get(m)
                r = stored.record
                members.append(
                    MemberDetail(
                        platform=m.platform,
                        job_id=m.job_id,
                        company=r.company,
                        position=r.position,
                        application_status=r.application_status.value,
                        posting_status=r.posting_status.value,
                    )
                )
                statuses.add(r.application_status.value)
            except JobRecordNotFound:
                members.append(
                    MemberDetail(
                        platform=m.platform,
                        job_id=m.job_id,
                        company=None,
                        position=None,
                        application_status=None,
                        posting_status=None,
                    )
                )
        return GroupDetail(
            group_id=group.group_id,
            members=tuple(members),
            note=group.note,
            inconsistent=len(statuses) > 1,
        )

    def _is_inconsistent(self, group: LinkGroup) -> bool:
        statuses: set[str] = set()
        for m in group.members:
            try:
                stored = self._repo.get(m)
                statuses.add(stored.record.application_status.value)
            except JobRecordNotFound:
                pass
        return len(statuses) > 1
