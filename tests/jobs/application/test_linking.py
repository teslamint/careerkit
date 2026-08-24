"""Tests for LinkService CRUD and sync logic."""

from __future__ import annotations

from pathlib import Path

import pytest

from careerkit.jobs.adapters.storage.file_records import JDRecordRepository
from careerkit.jobs.adapters.storage.link_store import LinkStore
from careerkit.jobs.application.linking import LinkService
from careerkit.jobs.domain.model import (
    ApplicationStatus,
    JobKey,
    JobRecord,
    PostingStatus,
)


def _make_record(platform: str, job_id: str, company: str = "TestCo", position: str = "Backend") -> JobRecord:
    return JobRecord(
        platform=platform,
        job_id=job_id,
        company=company,
        position=position,
    )


@pytest.fixture
def workspace(tmp_path: Path):
    records_dir = tmp_path / "records"
    links_dir = tmp_path / "links"
    return records_dir, links_dir


@pytest.fixture
def service(workspace):
    records_dir, links_dir = workspace
    repo = JDRecordRepository(records_dir)
    r1 = _make_record("saramin", "111", company="Acme", position="Backend Engineer")
    r2 = _make_record("wanted", "222", company="Acme", position="Backend Engineer")
    repo.create(r1, jd_markdown="# JD 1")
    repo.create(r2, jd_markdown="# JD 2")
    store = LinkStore(links_dir)
    return LinkService(link_store=store, record_repo=repo)


# --- U3: CRUD tests ---

def test_add_link(service: LinkService):
    k1 = JobKey("saramin", "111")
    k2 = JobKey("wanted", "222")
    result = service.add_link([k1, k2])
    assert result.created is True
    assert result.group_id is not None
    assert len(result.warnings) == 0


def test_add_link_warns_missing_record(workspace):
    records_dir, links_dir = workspace
    repo = JDRecordRepository(records_dir)
    repo.create(_make_record("saramin", "111"), jd_markdown="# JD")
    store = LinkStore(links_dir)
    svc = LinkService(link_store=store, record_repo=repo)

    result = svc.add_link([JobKey("saramin", "111"), JobKey("wanted", "999")])
    assert result.created is True
    assert len(result.warnings) > 0
    assert "999" in result.warnings[0]


def test_add_same_group_is_noop(service: LinkService):
    k1 = JobKey("saramin", "111")
    k2 = JobKey("wanted", "222")
    service.add_link([k1, k2])
    result = service.add_link([k1, k2])
    assert result.created is False


def test_add_link_different_group_raises(service: LinkService):
    k1 = JobKey("saramin", "111")
    k2 = JobKey("wanted", "222")
    service.add_link([k1, k2])
    with pytest.raises(ValueError, match="이미.*그룹"):
        service.add_link([k1, JobKey("remember", "333")])


def test_show_link(service: LinkService):
    k1 = JobKey("saramin", "111")
    k2 = JobKey("wanted", "222")
    service.add_link([k1, k2])
    detail = service.show_link(k1)
    assert detail is not None
    assert len(detail.members) == 2
    assert any(m.company == "Acme" for m in detail.members)


def test_show_link_not_found(service: LinkService):
    assert service.show_link(JobKey("x", "99")) is None


def test_list_links(service: LinkService):
    service.add_link([JobKey("saramin", "111"), JobKey("wanted", "222")])
    summaries = service.list_links()
    assert len(summaries) == 1
    assert summaries[0].member_count == 2


def test_list_links_inconsistent_filter(workspace):
    records_dir, links_dir = workspace
    repo = JDRecordRepository(records_dir)
    r1 = _make_record("saramin", "111")
    r2 = _make_record("wanted", "222")
    r3 = _make_record("a", "333", company="OtherCo", position="FE")
    r4 = _make_record("b", "444", company="OtherCo", position="FE")
    repo.create(r1, jd_markdown="# JD")
    repo.create(r2, jd_markdown="# JD")
    repo.create(r3, jd_markdown="# JD")
    repo.create(r4, jd_markdown="# JD")
    store = LinkStore(links_dir)
    svc = LinkService(link_store=store, record_repo=repo)

    svc.add_link([JobKey("saramin", "111"), JobKey("wanted", "222")])
    svc.add_link([JobKey("a", "333"), JobKey("b", "444")])

    repo.update_status(JobKey("saramin", "111"), application_status=ApplicationStatus.APPLIED)

    all_groups = svc.list_links()
    assert len(all_groups) == 2
    inconsistent = svc.list_links(inconsistent_only=True)
    assert len(inconsistent) == 1


def test_remove_link(service: LinkService):
    k1 = JobKey("saramin", "111")
    k2 = JobKey("wanted", "222")
    service.add_link([k1, k2])
    result = service.remove_link(k1)
    assert result is not None
    assert result.group_deleted is True
    assert service.show_link(k1) is None


def test_remove_link_not_found(service: LinkService):
    assert service.remove_link(JobKey("x", "99")) is None


def test_check_membership(service: LinkService):
    k1 = JobKey("saramin", "111")
    k2 = JobKey("wanted", "222")
    service.add_link([k1, k2])
    assert service.check_membership(k1) is not None
    assert service.check_membership(JobKey("x", "99")) is None


# --- U4: Sync tests ---

def test_sync_propagates_highest_status(workspace):
    records_dir, links_dir = workspace
    repo = JDRecordRepository(records_dir)
    repo.create(_make_record("saramin", "111"), jd_markdown="# JD")
    repo.create(_make_record("wanted", "222"), jd_markdown="# JD")
    store = LinkStore(links_dir)
    svc = LinkService(link_store=store, record_repo=repo)

    svc.add_link([JobKey("saramin", "111"), JobKey("wanted", "222")])
    repo.update_status(JobKey("saramin", "111"), application_status=ApplicationStatus.APPLIED)

    result = svc.sync(JobKey("saramin", "111"))
    assert len(result.changes) == 1
    assert result.changes[0].to_status == ApplicationStatus.APPLIED

    updated = repo.get(JobKey("wanted", "222"))
    assert updated.record.application_status == ApplicationStatus.APPLIED


def test_sync_excludes_rejected_source_and_target(workspace):
    records_dir, links_dir = workspace
    repo = JDRecordRepository(records_dir)
    repo.create(_make_record("a", "1"), jd_markdown="# JD")
    repo.create(_make_record("b", "2"), jd_markdown="# JD")
    repo.create(_make_record("c", "3"), jd_markdown="# JD")
    store = LinkStore(links_dir)
    svc = LinkService(link_store=store, record_repo=repo)

    svc.add_link([JobKey("a", "1"), JobKey("b", "2"), JobKey("c", "3")])
    repo.update_status(JobKey("a", "1"), application_status=ApplicationStatus.REJECTED)
    repo.update_status(JobKey("b", "2"), application_status=ApplicationStatus.INTERVIEW)

    result = svc.sync(JobKey("b", "2"))
    for change in result.changes:
        assert change.key != JobKey("a", "1")
    assert any(c.key == JobKey("c", "3") and c.to_status == ApplicationStatus.INTERVIEW for c in result.changes)

    rejected = repo.get(JobKey("a", "1"))
    assert rejected.record.application_status == ApplicationStatus.REJECTED


def test_sync_posting_status_closed(workspace):
    records_dir, links_dir = workspace
    repo = JDRecordRepository(records_dir)
    repo.create(_make_record("a", "1"), jd_markdown="# JD")
    repo.create(_make_record("b", "2"), jd_markdown="# JD")
    store = LinkStore(links_dir)
    svc = LinkService(link_store=store, record_repo=repo)

    svc.add_link([JobKey("a", "1"), JobKey("b", "2")])
    repo.update_status(JobKey("a", "1"), posting_status=PostingStatus.CLOSED)

    result = svc.sync(JobKey("a", "1"))
    assert any(c.posting_status_change == PostingStatus.CLOSED for c in result.changes)

    updated = repo.get(JobKey("b", "2"))
    assert updated.record.posting_status == PostingStatus.CLOSED


def test_sync_dry_run(workspace):
    records_dir, links_dir = workspace
    repo = JDRecordRepository(records_dir)
    repo.create(_make_record("a", "1"), jd_markdown="# JD")
    repo.create(_make_record("b", "2"), jd_markdown="# JD")
    store = LinkStore(links_dir)
    svc = LinkService(link_store=store, record_repo=repo)

    svc.add_link([JobKey("a", "1"), JobKey("b", "2")])
    repo.update_status(JobKey("a", "1"), application_status=ApplicationStatus.APPLIED)

    result = svc.sync(JobKey("a", "1"), dry_run=True)
    assert len(result.changes) == 1

    still_pending = repo.get(JobKey("b", "2"))
    assert still_pending.record.application_status == ApplicationStatus.PENDING


def test_sync_no_changes_needed(workspace):
    records_dir, links_dir = workspace
    repo = JDRecordRepository(records_dir)
    repo.create(_make_record("a", "1"), jd_markdown="# JD")
    repo.create(_make_record("b", "2"), jd_markdown="# JD")
    store = LinkStore(links_dir)
    svc = LinkService(link_store=store, record_repo=repo)

    svc.add_link([JobKey("a", "1"), JobKey("b", "2")])

    result = svc.sync(JobKey("a", "1"))
    assert len(result.changes) == 0


def test_sync_all_rejected(workspace):
    records_dir, links_dir = workspace
    repo = JDRecordRepository(records_dir)
    repo.create(_make_record("a", "1"), jd_markdown="# JD")
    repo.create(_make_record("b", "2"), jd_markdown="# JD")
    store = LinkStore(links_dir)
    svc = LinkService(link_store=store, record_repo=repo)

    svc.add_link([JobKey("a", "1"), JobKey("b", "2")])
    repo.update_status(JobKey("a", "1"), application_status=ApplicationStatus.REJECTED)
    repo.update_status(JobKey("b", "2"), application_status=ApplicationStatus.REJECTED)

    result = svc.sync(JobKey("a", "1"))
    assert len(result.changes) == 0


def test_sync_missing_member(workspace):
    records_dir, links_dir = workspace
    repo = JDRecordRepository(records_dir)
    repo.create(_make_record("a", "1"), jd_markdown="# JD")
    store = LinkStore(links_dir)

    store.root.mkdir(parents=True, exist_ok=True)
    from careerkit.jobs.domain.link_model import LinkGroup
    group = LinkGroup(
        group_id="deadbeef",
        members=(JobKey("a", "1"), JobKey("b", "missing")),
        created_at="2026-08-25T00:00:00+09:00",
    )
    store._save_group(group)

    svc = LinkService(link_store=store, record_repo=repo)
    result = svc.sync(JobKey("a", "1"))
    assert len(result.warnings) > 0


def test_sync_partial_failure_then_rerun_converges(workspace):
    records_dir, links_dir = workspace
    repo = JDRecordRepository(records_dir)
    repo.create(_make_record("a", "1"), jd_markdown="# JD")
    repo.create(_make_record("b", "2"), jd_markdown="# JD")
    repo.create(_make_record("c", "3"), jd_markdown="# JD")
    store = LinkStore(links_dir)
    svc = LinkService(link_store=store, record_repo=repo)

    svc.add_link([JobKey("a", "1"), JobKey("b", "2"), JobKey("c", "3")])
    repo.update_status(JobKey("a", "1"), application_status=ApplicationStatus.APPLIED)

    original_update = repo.update_status
    call_count = 0

    def failing_on_second(key, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise OSError("injected update_status failure")
        return original_update(key, **kwargs)

    repo.update_status = failing_on_second
    with pytest.raises(OSError, match="injected"):
        svc.sync(JobKey("a", "1"))

    repo.update_status = original_update
    result = svc.sync(JobKey("a", "1"))
    for m_key in [JobKey("b", "2"), JobKey("c", "3")]:
        rec = repo.get(m_key)
        assert rec.record.application_status == ApplicationStatus.APPLIED
    assert all(c.to_status is None or c.to_status == ApplicationStatus.APPLIED for c in result.changes)


def test_add_link_warns_company_mismatch(workspace):
    records_dir, links_dir = workspace
    repo = JDRecordRepository(records_dir)
    repo.create(
        JobRecord(platform="a", job_id="1", company="AlphaCo", position="BE"),
        jd_markdown="# JD",
    )
    repo.create(
        JobRecord(platform="b", job_id="2", company="BetaCo", position="BE"),
        jd_markdown="# JD",
    )
    store = LinkStore(links_dir)
    svc = LinkService(link_store=store, record_repo=repo)

    result = svc.add_link([JobKey("a", "1"), JobKey("b", "2")])
    assert result.created is True
    assert any("불일치" in w for w in result.warnings)


def test_sync_unlinked_key_raises(service: LinkService):
    with pytest.raises(ValueError, match="소속.*없"):
        service.sync(JobKey("x", "99"))
