from __future__ import annotations

import json
from pathlib import Path

import yaml

from careerkit.jobs.adapters.config_files import YamlConfigFileAdapter
from careerkit.jobs.adapters.storage.file_records import JDRecordRepository
from careerkit.jobs.application.config import SearchConfigService
from careerkit.jobs.application.preflight import WorkspacePreflightService
from careerkit.jobs.domain.model import (
    ApplicationEvent,
    ApplicationStatus,
    JobKey,
    JobRecord,
    PostingStatus,
    ScreeningVerdict,
)


LEGACY_RAW = {
    "platforms": {
        "wanted": {"enabled": True, "job_group_id": 518, "job_ids": [872]},
        "remember": {
            "enabled": True,
            "job_category_names": [{"level1": "SW개발", "level2": "백엔드"}],
        },
        "groupby": {"enabled": True, "position_types": [2]},
    },
    "search_queries": ["백엔드 엔지니어", "Senior Backend"],
    "execution": {"max_urls_per_run": 50},
}


def _config_service(config_path: Path) -> SearchConfigService:
    return SearchConfigService(YamlConfigFileAdapter(config_path))


def test_config_check_is_read_only_and_reports_exact_backend_mapping(tmp_path: Path) -> None:
    config_path = tmp_path / "jd" / "config" / "search_config.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    original = yaml.safe_dump(LEGACY_RAW, allow_unicode=True, sort_keys=False)
    config_path.write_text(original, encoding="utf-8")
    repository = JDRecordRepository(tmp_path / "jd" / "records")
    service = WorkspacePreflightService(
        config_service=_config_service(config_path),
        repository=repository,
        derived_root=tmp_path / "jd" / "derived",
        temp_root=tmp_path / "tmp",
    )

    result = service.check_config()

    assert result.ready is False
    assert result.action == "apply"
    assert result.normalized_role == "backend"
    assert any(item.code == "legacy_native_role_mapping" for item in result.findings)
    assert config_path.read_text(encoding="utf-8") == original


def test_config_backup_rejects_symlink_and_existing_rollback_backup(tmp_path: Path) -> None:
    config_path = tmp_path / "search_config.yaml"
    config_path.write_text("search:\n  role: backend\n", encoding="utf-8")
    adapter = YamlConfigFileAdapter(config_path)

    backup = adapter.backup()
    assert backup.exists()
    try:
        adapter.backup()
    except FileExistsError as exc:
        assert "rollback backup already exists" in str(exc)
    else:
        raise AssertionError("expected backup collision to be rejected")

    linked_path = tmp_path / "linked.yaml"
    linked_path.symlink_to(config_path)
    linked = YamlConfigFileAdapter(linked_path)
    try:
        linked.read()
    except ValueError as exc:
        assert "regular file" in str(exc)
    else:
        raise AssertionError("expected symlinked config to be rejected")


def test_storage_preflight_validates_metadata_and_rebuilds_to_isolated_output(tmp_path: Path) -> None:
    records_root = tmp_path / "jd" / "records"
    repository = JDRecordRepository(records_root)
    repository.create(
        JobRecord(
            "wanted",
            "123",
            "Acme",
            "Backend",
            source_url="https://www.wanted.co.kr/wd/123",
            screening_verdict=ScreeningVerdict.RECOMMENDED,
        ),
        jd_markdown="# JD\n",
    )
    repository.create(
        JobRecord(
            "remember",
            "124",
            "Beta",
            "Platform",
            source_url="https://career.rememberapp.co.kr/job/posting/124",
            application_status=ApplicationStatus.APPLIED,
            posting_status=PostingStatus.CLOSED,
        ),
        jd_markdown="# JD\n",
    )
    repository.update_screening_result(JobKey("wanted", "123"), screening_markdown="# Screening\n")
    config_path = tmp_path / "jd" / "config" / "search_config.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("search:\n  role: backend\n", encoding="utf-8")
    active_derived = tmp_path / "jd" / "derived"
    active_derived.mkdir(parents=True, exist_ok=True)
    marker = active_derived / "existing.txt"
    marker.write_text("untouched", encoding="utf-8")
    service = WorkspacePreflightService(
        config_service=_config_service(config_path),
        repository=repository,
        derived_root=active_derived,
        temp_root=tmp_path / "tmp",
    )

    result = service.preflight_storage()

    assert result.ready is True
    assert result.record_count == 2
    assert result.screening_count == 1
    assert result.checked_keys == ("remember:124", "wanted:123")
    assert result.status_counts["screening:recommended"] == 1
    assert result.status_counts["screening:unscreened"] == 1
    assert result.status_counts["application:applied"] == 1
    assert result.status_counts["posting:closed"] == 1
    assert result.isolated_output_root.parent == tmp_path / "tmp"
    assert (result.isolated_output_root / "derived" / "jd.sqlite3").exists()
    assert marker.read_text(encoding="utf-8") == "untouched"
    assert repository.get(JobKey("wanted", "123")).jd_markdown == "# JD\n"
    service.cleanup_isolated_output(result.isolated_output_root)
    assert not result.isolated_output_root.exists()


def test_storage_preflight_reports_integrity_failures_without_emitting_bodies(tmp_path: Path) -> None:
    records_root = tmp_path / "jd" / "records"
    repository = JDRecordRepository(records_root)
    record = JobRecord(
        "wanted",
        "999",
        "Gamma",
        "Infra",
        source_url="https://www.wanted.co.kr/wd/999",
    )
    repository.create(record, jd_markdown="# sensitive body\n")
    manifest_path = records_root / "wanted" / "999" / "record.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    jd_path = records_root / "wanted" / "999" / manifest["content"]["jd_path"]
    jd_path.write_text("tampered", encoding="utf-8")
    config_path = tmp_path / "jd" / "config" / "search_config.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("search:\n  role: backend\n", encoding="utf-8")
    service = WorkspacePreflightService(
        config_service=_config_service(config_path),
        repository=repository,
        derived_root=tmp_path / "jd" / "derived",
        temp_root=tmp_path / "tmp",
    )

    result = service.preflight_storage()

    assert result.ready is False
    assert any(item.code == "integrity_error" for item in result.findings)
    assert all("sensitive body" not in item.message for item in result.findings)


def test_storage_preflight_reports_malformed_manifest_per_record(tmp_path: Path) -> None:
    records_root = tmp_path / "jd" / "records"
    repository = JDRecordRepository(records_root)
    repository.create(
        JobRecord("wanted", "broken", "Broken Co", "Backend"),
        jd_markdown="# private body\n",
    )
    (records_root / "wanted" / "broken" / "record.json").write_text(
        "{not-json",
        encoding="utf-8",
    )
    config_path = tmp_path / "jd" / "config" / "search_config.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("search:\n  role: backend\n", encoding="utf-8")
    service = WorkspacePreflightService(
        config_service=_config_service(config_path),
        repository=repository,
        derived_root=tmp_path / "jd" / "derived",
        temp_root=tmp_path / "tmp",
    )

    result = service.preflight_storage()

    assert result.ready is False
    assert result.record_count == 1
    assert result.checked_keys == ("wanted:broken",)
    assert any(
        item.code == "integrity_error" and item.target == "wanted:broken"
        for item in result.findings
    )
    assert all("private body" not in item.message for item in result.findings)




def test_storage_preflight_aggregates_application_timestamp_categories(tmp_path: Path) -> None:
    records_root = tmp_path / "jd" / "records"
    repository = JDRecordRepository(records_root)
    repository.create(
        JobRecord("wanted", "absent", "Acme", "Backend"),
        jd_markdown="# JD\n",
    )
    repository.create(
        JobRecord(
            "wanted",
            "aware",
            "Aware",
            "Backend",
            application_status=ApplicationStatus.APPLIED,
            application_status_updated_at="2026-07-14T03:00:00+09:00",
            application_history=(
                ApplicationEvent(
                    status=ApplicationStatus.APPLIED,
                    occurred_at="2026-07-14T03:00:00+09:00",
                    note=None,
                ),
            ),
        ),
        jd_markdown="# JD\n",
    )
    repository.create(
        JobRecord(
            "wanted",
            "naive",
            "Naive",
            "Backend",
            application_status=ApplicationStatus.APPLIED,
            application_status_updated_at="2026-07-14T03:00:00",
            application_history=(
                ApplicationEvent(
                    status=ApplicationStatus.APPLIED,
                    occurred_at="2026-07-14T03:00:00",
                    note=None,
                ),
            ),
        ),
        jd_markdown="# JD\n",
    )
    repository.create(
        JobRecord(
            "wanted",
            "invalid",
            "Invalid",
            "Backend",
            application_status=ApplicationStatus.APPLIED,
        ),
        jd_markdown="# JD\n",
    )
    invalid_manifest = records_root / "wanted" / "invalid" / "record.json"
    invalid_payload = json.loads(invalid_manifest.read_text(encoding="utf-8"))
    invalid_payload["schema_version"] = 1
    invalid_payload["record"]["schema_version"] = 1
    invalid_payload["record"]["application_status_updated_at"] = "not-a-timestamp"
    invalid_payload["record"].pop("application_history", None)
    invalid_manifest.write_text(
        json.dumps(invalid_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    config_path = tmp_path / "jd" / "config" / "search_config.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("search:\n  role: backend\n", encoding="utf-8")
    service = WorkspacePreflightService(
        config_service=_config_service(config_path),
        repository=repository,
        derived_root=tmp_path / "jd" / "derived",
        temp_root=tmp_path / "tmp",
    )

    result = service.preflight_storage()

    assert result.ready is True
    assert result.application_timestamp_categories == {
        "absent": 1,
        "aware": 1,
        "invalid": 1,
        "naive": 1,
    }
