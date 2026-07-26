from __future__ import annotations

import hashlib
from pathlib import Path

from careerkit.jobs.adapters.storage.file_records import JDRecordRepository
from careerkit.jobs.adapters.storage.sqlite_index import JDSearchIndex
from careerkit.jobs.application.storage_migration import MigrationPaths, StorageMigrator
from careerkit.jobs.domain.model import JobKey


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_clean_cutover_preserves_sources_and_rebuilds_index(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy-private"
    stage = tmp_path / "stage"
    active = tmp_path / "active-jd"
    report_path = tmp_path / "report.json"
    jd = _write(
        legacy / "job_postings/conditional/high/70-acme-backend.md",
        "# Acme Backend\n\n| 회사명 | Acme |\n| 포지션 | Backend |\n| 출처 | [공고](https://www.wanted.co.kr/wd/70) |\n",
    )
    screening = _write(
        legacy / "jd_analysis/screening/70-acme-backend.md",
        "# Screening\n\n### 최종 판정: 지원 추천\n",
    )
    before = {_sha256(jd), _sha256(screening)}
    migrator = StorageMigrator(MigrationPaths(legacy, stage, active, report_path))

    preflight = migrator.preflight()
    activated = migrator.activate(preflight)
    repository = JDRecordRepository(active / "records")
    index_path = active / "derived/search.sqlite3"
    index = JDSearchIndex(index_path, repository)

    assert preflight.ready and not preflight.blockers
    assert activated.activated
    assert (active / "runtime/migration-report.json").exists()
    assert repository.get(JobKey("wanted", "70")).screening_markdown == screening.read_text(encoding="utf-8")
    assert index.rebuild().success
    first_keys = {(item.platform, item.job_id) for item in index.search(limit=100).items}

    index_path.unlink()
    assert index.rebuild().success

    assert {(item.platform, item.job_id) for item in index.search(limit=100).items} == first_keys
    assert {_sha256(jd), _sha256(screening)} == before
