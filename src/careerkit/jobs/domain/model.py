"""Domain model for canonical JD file records."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import re
from typing import Any, Mapping


SCHEMA_VERSION = 1
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ScreeningVerdict(str, Enum):
    RECOMMENDED = "recommended"
    HOLD = "hold"
    NOT_RECOMMENDED = "not_recommended"


class ApplicationStatus(str, Enum):
    PENDING = "pending"
    APPLIED = "applied"
    INTERVIEW = "interview"
    OFFER = "offer"
    REJECTED = "rejected"


class PostingStatus(str, Enum):
    ACTIVE = "active"
    CLOSED = "closed"


def _validate_component(value: str, *, field: str) -> str:
    if not _SAFE_COMPONENT.fullmatch(value) or value in {".", ".."}:
        raise ValueError(f"Invalid {field}: {value!r}")
    return value


@dataclass(frozen=True, order=True)
class JobKey:
    platform: str
    job_id: str

    def __post_init__(self) -> None:
        _validate_component(self.platform, field="platform")
        _validate_component(self.job_id, field="job_id")


@dataclass(frozen=True)
class JobRecord:
    platform: str
    job_id: str
    company: str
    position: str
    source_url: str = ""
    screening_verdict: ScreeningVerdict | None = None
    application_status: ApplicationStatus = ApplicationStatus.PENDING
    posting_status: PostingStatus = PostingStatus.ACTIVE
    application_status_updated_at: str | None = None
    migration_source: str | None = None
    screening_provider: str | None = None
    verdict_capped: bool = False
    prescreen_reason: str | None = None
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        JobKey(self.platform, self.job_id)
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported schema_version: {self.schema_version}; expected {SCHEMA_VERSION}"
            )
        if not self.company.strip():
            raise ValueError("company must not be blank")
        if not self.position.strip():
            raise ValueError("position must not be blank")

    @property
    def key(self) -> JobKey:
        return JobKey(self.platform, self.job_id)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["screening_verdict"] = (
            self.screening_verdict.value if self.screening_verdict is not None else None
        )
        data["application_status"] = self.application_status.value
        data["posting_status"] = self.posting_status.value
        return data

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "JobRecord":
        try:
            verdict_raw = raw.get("screening_verdict")
            return cls(
                platform=str(raw["platform"]),
                job_id=str(raw["job_id"]),
                company=str(raw["company"]),
                position=str(raw["position"]),
                source_url=str(raw.get("source_url", "")),
                screening_verdict=(
                    ScreeningVerdict(str(verdict_raw)) if verdict_raw is not None else None
                ),
                application_status=ApplicationStatus(
                    str(raw.get("application_status", ApplicationStatus.PENDING.value))
                ),
                posting_status=PostingStatus(
                    str(raw.get("posting_status", PostingStatus.ACTIVE.value))
                ),
                application_status_updated_at=_optional_string(
                    raw.get("application_status_updated_at")
                ),
                migration_source=_optional_string(raw.get("migration_source")),
                screening_provider=_optional_string(raw.get("screening_provider")),
                verdict_capped=_strict_bool(raw.get("verdict_capped", False)),
                prescreen_reason=_optional_string(raw.get("prescreen_reason")),
                schema_version=int(raw.get("schema_version", SCHEMA_VERSION)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Invalid job record: {exc}") from exc


def _strict_bool(value: Any) -> bool:
    # Not bool(): the JSON string "false" is truthy, and a record claiming a cap it
    # does not have would be picked up and republished by `queue capped --rescreen`.
    if not isinstance(value, bool):
        raise ValueError(f"expected boolean, got {type(value).__name__}")
    return value


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"expected string or null, got {type(value).__name__}")
    return value
