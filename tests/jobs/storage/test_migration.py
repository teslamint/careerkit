from __future__ import annotations

import json
from pathlib import Path

from careerkit.jobs.application.storage_migration import MigrationPaths, StorageMigrator
from careerkit.jobs.application.pipeline import JobsPipelineService
from careerkit.jobs.domain.model import ApplicationEvent, ApplicationStatus, JobKey, SCHEMA_VERSION, ScreeningVerdict
from careerkit.jobs.adapters.storage.file_records import JDRecordRepository


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _paths(tmp_path: Path) -> MigrationPaths:
    return MigrationPaths(
        legacy_private=tmp_path / "legacy-private",
        stage_root=tmp_path / "stage",
        active_root=tmp_path / "active-jd",
        report_path=tmp_path / "migration-report.json",
    )


def _jd(url: str, company: str = "Acme", position: str = "Backend") -> str:
    return (
        f"# {company} {position}\n\n"
        f"| 회사명 | {company} |\n"
        f"| 포지션 | {position} |\n"
        f"| 출처 | [공고]({url}) |\n"
    )


def test_preflight_stages_records_and_runtime_metadata_without_touching_active_root(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _write(paths.legacy_private / "job_postings/conditional/high/70-acme-backend.md", _jd("https://www.wanted.co.kr/wd/70"))
    _write(paths.legacy_private / "jd_analysis/screening/70-acme-backend.md", "# Screening\n\n### 최종 판정: 지원 추천\n")
    _write(paths.legacy_private / "job_postings/search_config.yaml", "platforms: {}\n")
    _write(paths.legacy_private / "job_postings/queue.json", json.dumps([{"job_id": "70", "url": "https://www.wanted.co.kr/wd/70"}]))
    _write(
        paths.legacy_private / "job_postings/.search_state.json",
        json.dumps({"seen_job_ids": ["70"]}),
    )

    report = StorageMigrator(paths).preflight()
    repository = JDRecordRepository(paths.stage_root / "records")
    stored = repository.get(JobKey("wanted", "70"))

    assert report.ready
    assert not paths.active_root.exists()
    assert stored.record.screening_verdict is ScreeningVerdict.RECOMMENDED
    assert stored.screening_markdown is not None
    assert (paths.stage_root / "config" / "search_config.yaml").exists()
    assert json.loads(
        (paths.stage_root / "runtime" / "queue" / "queue.json").read_text(encoding="utf-8")
    ) == [
        {
            "job_id": "70",
            "platform": "wanted",
            "url": "https://www.wanted.co.kr/wd/70",
        }
    ]
    assert json.loads(
        (paths.stage_root / "runtime" / "search_state.json").read_text(encoding="utf-8")
    ) == {"seen_job_keys": ["wanted:70"]}
    queue = JobsPipelineService(
        workspace_root=tmp_path,
        repository=repository,
        runtime_dir=paths.stage_root / "runtime",
    ).queue_status()
    assert queue.total == 1
    assert queue.counts == {"pending": 1}


def test_screening_only_input_creates_placeholder_record_and_notice(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    screening = _write(
        paths.legacy_private / "jd_analysis/screening/headhunter-99-acme-backend.md",
        "# Screening\n\n### 최종 판정: 지원 보류\n",
    )

    report = StorageMigrator(paths).preflight()
    records = JDRecordRepository(paths.stage_root / "records").list()

    assert report.ready
    assert len(records) == 1
    assert records[0].record.platform == "headhunter"
    assert "원본 JD 파일이 레거시 저장소에 없어" in records[0].jd_markdown
    assert records[0].screening_markdown == screening.read_text(encoding="utf-8")
    assert records[0].record.screening_verdict is ScreeningVerdict.HOLD
    assert {finding.code for finding in report.notices} >= {"screening_only_placeholder_created"}


def test_preflight_parses_legacy_frontmatter_and_bullet_metadata(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _write(
        paths.legacy_private / "job_postings/72-acme-backend.md",
        "---\ncompany: Frontmatter Co\n---\n"
        "# Backend Engineer\n\n"
        "- **포지션**: Platform Backend\n"
        "- **경력**: 5년 이상\n"
        "- 근무지: 서울\n\n"
        "출처: [공고](https://www.wanted.co.kr/wd/72)\n",
    )

    report = StorageMigrator(paths).preflight()
    stored = JDRecordRepository(paths.stage_root / "records").get(JobKey("wanted", "72"))

    assert report.ready
    assert stored.record.company == "Frontmatter Co"
    assert stored.record.position == "Platform Backend"


def test_activate_publishes_stage_atomically_and_writes_runtime_report(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _write(paths.legacy_private / "job_postings/applied/71-acme-backend.md", "---\nstatus: applied\nstatus_updated: 2026-07-01\n---\n" + _jd("https://www.wanted.co.kr/wd/71"))
    migrator = StorageMigrator(paths)

    preflight = migrator.preflight()
    activated = migrator.activate(preflight)
    stored = JDRecordRepository(paths.active_root / "records").get(JobKey("wanted", "71"))

    assert activated.activated is True
    assert stored.record.application_status is ApplicationStatus.APPLIED
    assert stored.record.application_status_updated_at == "2026-07-01"
    assert (paths.active_root / "runtime" / "migration-report.json").exists()




def test_preflight_writes_canonical_schema_v2_with_synthesized_history(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _write(
        paths.legacy_private / "job_postings/applied/73-acme-backend.md",
        "---\nstatus: applied\nstatus_updated: 2026-07-01T09:00:00+09:00\n---\n" + _jd("https://www.wanted.co.kr/wd/73"),
    )

    report = StorageMigrator(paths).preflight()
    stored = JDRecordRepository(paths.stage_root / "records").get(JobKey("wanted", "73"))

    assert report.ready is True
    assert stored.record.schema_version == SCHEMA_VERSION
    assert stored.record.application_history == (
        ApplicationEvent(
            status=ApplicationStatus.APPLIED,
            occurred_at="2026-07-01T09:00:00+09:00",
            note=None,
        ),
    )
    record_json = json.loads(
        (paths.stage_root / "records" / "wanted" / "73" / "record.json").read_text(encoding="utf-8")
    )
    assert record_json["record"]["schema_version"] == 2
    assert record_json["record"]["application_history"] == [
        {
            "status": "applied",
            "occurred_at": "2026-07-01T09:00:00+09:00",
            "note": None,
        }
    ]



def test_migration_report_keeps_schema_version_1(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _write(
        paths.legacy_private / "job_postings/74-acme-backend.md",
        _jd("https://www.wanted.co.kr/wd/74"),
    )

    report = StorageMigrator(paths).preflight()
    report_payload = json.loads(paths.report_path.read_text(encoding="utf-8"))

    assert report.ready is True
    assert report_payload["schema_version"] == 1
