from __future__ import annotations

from typing import Optional

from careerkit.jobs.adapters.storage.file_records import JDRecordRepository

STATUS_ALIASES = {
    "pending": "pending",
    "보류": "pending",
    "조건부": "pending",
    "조건부(상)": "pending",
    "조건부(중)": "pending",
    "조건부(하)": "pending",
    "조건부(보류)": "pending",
    "우선": "pending",
    "보류 / 패스": "pending",
    "pass": "rejected",
    "패스": "rejected",
    "rejected": "rejected",
    "applied": "applied",
    "지원": "applied",
    "interview": "interview",
    "면접": "interview",
    "offer": "offer",
    "오퍼": "offer",
}


def parse_frontmatter(content: str) -> dict[str, str]:
    if not content.startswith("---"):
        return {}
    lines = content.split("\n")
    if len(lines) < 2:
        return {}
    end_idx = -1
    for i, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            end_idx = i
            break
    if end_idx == -1:
        return {}
    result: dict[str, str] = {}
    for line in lines[1:end_idx]:
        if ":" in line:
            key, value = line.split(":", 1)
            result[key.strip()] = value.strip()
    return result


def normalize_status(status: Optional[str]) -> Optional[str]:
    if status is None:
        return None
    status_clean = str(status).strip().strip("'\"")
    if not status_clean:
        return None
    if status_clean in {"pending", "applied", "rejected", "interview", "offer"}:
        return status_clean
    status_lower = status_clean.lower()
    if status_lower in STATUS_ALIASES:
        return STATUS_ALIASES[status_lower]
    return STATUS_ALIASES.get(status_clean, status_clean)


def get_status(*, repository: JDRecordRepository) -> dict[str, int]:
    status: dict[str, int] = {}
    for item in repository.list():
        verdict = item.record.screening_verdict.value if item.record.screening_verdict else "unscreened"
        for label in (
            f"screening:{verdict}",
            f"application:{item.record.application_status.value}",
            f"posting:{item.record.posting_status.value}",
        ):
            status[label] = status.get(label, 0) + 1
    status["records:total"] = len(repository.list_metadata())
    return status


def migrate_status(*, repository: JDRecordRepository) -> list[dict[str, str]]:
    return [
        {
            "job_key": f"{item.record.platform}/{item.record.job_id}",
            "status": item.record.application_status.value,
            "result": "skipped",
        }
        for item in repository.list()
    ]
