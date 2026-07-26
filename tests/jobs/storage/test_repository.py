from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from careerkit.jobs.domain.model import (
    ApplicationStatus,
    JobKey,
    JobRecord,
    PostingStatus,
    ScreeningVerdict,
)
from careerkit.jobs.adapters.storage.file_records import (
    JDRecordRepository,
    JobRecordIntegrityError,
    JobRecordNotFound,
)


def _record(
    *,
    platform: str = "wanted",
    job_id: str = "100001",
    company: str = "Example",
    position: str = "Backend Engineer",
) -> JobRecord:
    return JobRecord(
        platform=platform,
        job_id=job_id,
        company=company,
        position=position,
        source_url=f"https://example.com/{platform}/{job_id}",
    )


def test_save_and_get_jd_only_record(tmp_path: Path) -> None:
    repo = JDRecordRepository(tmp_path)
    saved = repo.create(_record(), jd_markdown="# JD\nbody")

    fetched = repo.get(JobKey("wanted", "100001"))

    assert fetched == saved
    assert fetched.record.key == JobKey("wanted", "100001")
    assert fetched.jd_markdown == "# JD\nbody"
    assert fetched.screening_markdown is None


def test_add_screening_creates_new_revision_without_mutating_old_content(tmp_path: Path) -> None:
    repo = JDRecordRepository(tmp_path)
    repo.create(_record(), jd_markdown="# JD\nbody")
    first_manifest = json.loads((tmp_path / "wanted" / "100001" / "record.json").read_text())

    updated = repo.update_screening_result(JobKey("wanted", "100001"), screening_markdown="# Screening\nfit")
    second_manifest = json.loads((tmp_path / "wanted" / "100001" / "record.json").read_text())

    assert updated.screening_markdown == "# Screening\nfit"
    assert first_manifest["content"]["revision"] != second_manifest["content"]["revision"]
    assert (tmp_path / "wanted" / "100001" / second_manifest["content"]["jd_path"]).read_text() == "# JD\nbody"
    assert (tmp_path / "wanted" / "100001" / second_manifest["content"]["screening_path"]).read_text() == "# Screening\nfit"


def test_manifest_persists_compound_identity_and_content_hashes(tmp_path: Path) -> None:
    repo = JDRecordRepository(tmp_path)
    repo.create(_record(platform="wanted", job_id="42"), jd_markdown="# JD\nbody")
    repo.update_screening_result(JobKey("wanted", "42"), screening_markdown="# Screening\nfit")

    manifest_path = tmp_path / "wanted" / "42" / "record.json"
    manifest = json.loads(manifest_path.read_text())
    record_dir = manifest_path.parent
    jd_path = record_dir / manifest["content"]["jd_path"]
    screening_path = record_dir / manifest["content"]["screening_path"]

    assert manifest["record"]["platform"] == "wanted"
    assert manifest["record"]["job_id"] == "42"
    assert manifest["content"]["revision"]
    assert manifest["content"]["jd_sha256"] == hashlib.sha256(jd_path.read_bytes()).hexdigest()
    assert manifest["content"]["screening_sha256"] == hashlib.sha256(screening_path.read_bytes()).hexdigest()


def test_same_numeric_id_is_isolated_per_platform(tmp_path: Path) -> None:
    repo = JDRecordRepository(tmp_path)
    repo.create(_record(platform="wanted", job_id="42"), jd_markdown="wanted jd")
    repo.create(_record(platform="remember", job_id="42"), jd_markdown="remember jd")

    keys = [item.record.key for item in repo.list()]

    assert keys == [JobKey("remember", "42"), JobKey("wanted", "42")]
    assert repo.get(JobKey("wanted", "42")).jd_markdown == "wanted jd"
    assert repo.get(JobKey("remember", "42")).jd_markdown == "remember jd"


def test_traversal_rejection_happens_before_any_write(tmp_path: Path) -> None:
    repo = JDRecordRepository(tmp_path)

    with pytest.raises(ValueError, match="Invalid platform"):
        repo.create(_record(platform="../wanted"), jd_markdown="bad")

    assert list(tmp_path.rglob("*")) == []


@pytest.mark.parametrize(
    "mutator, expected",
    [
        ("missing_manifest", "Manifest not found"),
        ("corrupt_manifest", "Invalid manifest"),
        ("hash_mismatch", "Hash mismatch"),
    ],
)
def test_get_rejects_missing_or_corrupt_storage(tmp_path: Path, mutator: str, expected: str) -> None:
    repo = JDRecordRepository(tmp_path)
    repo.create(_record(), jd_markdown="body")
    record_dir = tmp_path / "wanted" / "100001"

    if mutator == "missing_manifest":
        (record_dir / "record.json").unlink()
    elif mutator == "corrupt_manifest":
        (record_dir / "record.json").write_text("not-json")
    else:
        manifest = json.loads((record_dir / "record.json").read_text())
        jd_path = record_dir / manifest["content"]["jd_path"]
        jd_path.write_text("tampered")

    with pytest.raises(JobRecordIntegrityError, match=expected):
        repo.get(JobKey("wanted", "100001"))


def test_status_update_changes_axes_without_rewriting_content(tmp_path: Path) -> None:
    repo = JDRecordRepository(tmp_path)
    repo.create(_record(), jd_markdown="body")
    before = json.loads((tmp_path / "wanted" / "100001" / "record.json").read_text())

    updated = repo.update_status(
        JobKey("wanted", "100001"),
        application_status=ApplicationStatus.APPLIED,
        posting_status=PostingStatus.CLOSED,
        application_status_updated_at="2026-07-13T12:00:00+09:00",
    )
    after = json.loads((tmp_path / "wanted" / "100001" / "record.json").read_text())

    assert updated.record.application_status is ApplicationStatus.APPLIED
    assert updated.record.posting_status is PostingStatus.CLOSED
    assert updated.record.application_status_updated_at == "2026-07-13T12:00:00+09:00"
    assert before["content"]["revision"] == after["content"]["revision"]


def test_verdict_update_preserves_newer_status_metadata(tmp_path: Path) -> None:
    repo = JDRecordRepository(tmp_path)
    repo.create(_record(), jd_markdown="body")
    repo.update_status(JobKey("wanted", "100001"), application_status=ApplicationStatus.APPLIED)

    updated = repo.update_verdict(JobKey("wanted", "100001"), ScreeningVerdict.RECOMMENDED)

    assert updated.record.screening_verdict is ScreeningVerdict.RECOMMENDED
    assert updated.record.application_status is ApplicationStatus.APPLIED


def test_screening_result_atomically_preserves_newer_status_metadata(tmp_path: Path) -> None:
    repo = JDRecordRepository(tmp_path)
    repo.create(_record(), jd_markdown="body")
    repo.update_status(JobKey("wanted", "100001"), posting_status=PostingStatus.CLOSED)

    updated = repo.update_screening_result(
        JobKey("wanted", "100001"),
        screening_markdown="# screening",
        screening_verdict=ScreeningVerdict.HOLD,
    )

    assert updated.record.screening_verdict is ScreeningVerdict.HOLD
    assert updated.record.posting_status is PostingStatus.CLOSED
    assert updated.screening_markdown == "# screening"


def test_update_operations_require_existing_record(tmp_path: Path) -> None:
    repo = JDRecordRepository(tmp_path)
    key = JobKey("wanted", "missing")

    with pytest.raises(JobRecordNotFound):
        repo.update_screening_result(key, screening_markdown="# screening")

    with pytest.raises(JobRecordNotFound):
        repo.update_status(key, application_status=ApplicationStatus.APPLIED)


def test_missing_record_has_distinct_required_and_optional_lookup_semantics(tmp_path: Path) -> None:
    repo = JDRecordRepository(tmp_path)
    key = JobKey("wanted", "missing")

    with pytest.raises(JobRecordNotFound):
        repo.get(key)

    assert repo.find(key) is None


def test_get_rejects_manifest_identity_mismatch(tmp_path: Path) -> None:
    repo = JDRecordRepository(tmp_path)
    repo.create(_record(), jd_markdown="body")
    manifest_path = tmp_path / "wanted" / "100001" / "record.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["record"]["platform"] = "remember"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(JobRecordIntegrityError, match="identity mismatch"):
        repo.get(JobKey("wanted", "100001"))


def test_provider_and_cap_default_to_absent_on_legacy_records(tmp_path: Path) -> None:
    repo = JDRecordRepository(tmp_path)
    repo.create(_record(), jd_markdown="body")
    manifest_path = tmp_path / "wanted" / "100001" / "record.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["record"].pop("screening_provider", None)
    manifest["record"].pop("verdict_capped", None)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False))

    fetched = repo.get(JobKey("wanted", "100001"))

    assert fetched.record.screening_provider is None
    assert fetched.record.verdict_capped is False
    assert fetched.record.schema_version == 1


def test_screening_result_persists_provider_and_cap(tmp_path: Path) -> None:
    repo = JDRecordRepository(tmp_path)
    repo.create(_record(), jd_markdown="body")

    repo.update_screening_result(
        JobKey("wanted", "100001"),
        screening_markdown="# screening",
        screening_verdict=ScreeningVerdict.HOLD,
        screening_provider="ollama",
        verdict_capped=True,
    )

    fetched = repo.get(JobKey("wanted", "100001"))
    assert fetched.record.screening_provider == "ollama"
    assert fetched.record.verdict_capped is True


def test_screening_result_keeps_cap_when_argument_omitted(tmp_path: Path) -> None:
    repo = JDRecordRepository(tmp_path)
    repo.create(_record(), jd_markdown="body")
    key = JobKey("wanted", "100001")
    repo.update_screening_result(
        key,
        screening_markdown="# first",
        screening_provider="ollama",
        verdict_capped=True,
    )

    repo.update_screening_result(key, screening_markdown="# second")

    fetched = repo.get(key)
    assert fetched.record.verdict_capped is True
    assert fetched.record.screening_provider == "ollama"


def test_screening_result_clears_cap_when_explicitly_false(tmp_path: Path) -> None:
    repo = JDRecordRepository(tmp_path)
    repo.create(_record(), jd_markdown="body")
    key = JobKey("wanted", "100001")
    repo.update_screening_result(
        key, screening_markdown="# first", screening_provider="ollama", verdict_capped=True
    )

    repo.update_screening_result(
        key, screening_markdown="# second", screening_provider="codex", verdict_capped=False
    )

    fetched = repo.get(key)
    assert fetched.record.verdict_capped is False
    assert fetched.record.screening_provider == "codex"


def test_record_dict_roundtrip_preserves_new_fields() -> None:
    record = _record()
    restored = JobRecord.from_dict(
        {**record.to_dict(), "screening_provider": "local", "verdict_capped": True}
    )

    assert restored.screening_provider == "local"
    assert restored.verdict_capped is True
    assert JobRecord.from_dict(restored.to_dict()) == restored
