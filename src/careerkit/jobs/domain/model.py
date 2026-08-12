"""Domain model for canonical JD file records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import re
from typing import Any, Mapping


SCHEMA_VERSION = 2
_LEGACY_SCHEMA_VERSION = 1
_SUPPORTED_SCHEMA_VERSIONS = {_LEGACY_SCHEMA_VERSION, SCHEMA_VERSION}
_MAX_APPLICATION_NOTE_LENGTH = 2000
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
class ApplicationEvent:
    status: ApplicationStatus
    occurred_at: str
    note: str | None = None

    def __post_init__(self) -> None:
        _parse_iso8601(self.occurred_at)
        object.__setattr__(self, "note", _normalize_note(self.note))

    def to_dict(self) -> dict[str, str | None]:
        return {
            "status": self.status.value,
            "occurred_at": self.occurred_at,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ApplicationEvent":
        allowed_fields = {"status", "occurred_at", "note"}
        unknown_fields = sorted(set(raw) - allowed_fields)
        if unknown_fields:
            raise ValueError(f"unknown fields: {', '.join(unknown_fields)}")
        try:
            return cls(
                status=ApplicationStatus(str(raw["status"])),
                occurred_at=_required_string(raw.get("occurred_at"), field="occurred_at"),
                note=_optional_string(raw.get("note")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Invalid application event: {exc}") from exc


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
    application_history: tuple[ApplicationEvent, ...] = ()
    migration_source: str | None = None
    screening_provider: str | None = None
    verdict_capped: bool = False
    prescreen_reason: str | None = None
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        JobKey(self.platform, self.job_id)
        if self.schema_version not in _SUPPORTED_SCHEMA_VERSIONS:
            raise ValueError(
                f"Unsupported schema_version: {self.schema_version}; expected 1 or {SCHEMA_VERSION}"
            )
        original_schema_version = self.schema_version
        if not self.company.strip():
            raise ValueError("company must not be blank")
        if not self.position.strip():
            raise ValueError("position must not be blank")
        history = self._normalize_history(self.application_history)
        if history:
            latest = history[-1]
            if (
                self.application_status is not latest.status
                or self.application_status_updated_at != latest.occurred_at
            ):
                raise ValueError("application_history must match current projection")
        elif original_schema_version == _LEGACY_SCHEMA_VERSION:
            legacy_event = _synthesize_legacy_event(
                self.application_status,
                self.application_status_updated_at,
            )
            if legacy_event is not None:
                history = (legacy_event,)
        elif self.application_status_updated_at is not None:
            try:
                _parse_iso8601(self.application_status_updated_at)
            except ValueError:
                pass
            else:
                raise ValueError(
                    "schema_version 2 requires application_history to own current application metadata"
                )
        object.__setattr__(self, "application_history", history)
        object.__setattr__(self, "schema_version", SCHEMA_VERSION)

    @staticmethod
    def _normalize_history(
        history: tuple[ApplicationEvent, ...] | list[ApplicationEvent],
    ) -> tuple[ApplicationEvent, ...]:
        normalized = tuple(history)
        for item in normalized:
            if not isinstance(item, ApplicationEvent):
                raise ValueError("application_history must contain ApplicationEvent values")
        return normalized

    @property
    def key(self) -> JobKey:
        return JobKey(self.platform, self.job_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "job_id": self.job_id,
            "company": self.company,
            "position": self.position,
            "source_url": self.source_url,
            "screening_verdict": (
                self.screening_verdict.value if self.screening_verdict is not None else None
            ),
            "application_status": self.application_status.value,
            "posting_status": self.posting_status.value,
            "application_status_updated_at": self.application_status_updated_at,
            "application_history": [item.to_dict() for item in self.application_history],
            "migration_source": self.migration_source,
            "screening_provider": self.screening_provider,
            "verdict_capped": self.verdict_capped,
            "prescreen_reason": self.prescreen_reason,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "JobRecord":
        try:
            verdict_raw = raw.get("screening_verdict")
            schema_version = _schema_version(raw.get("schema_version", _LEGACY_SCHEMA_VERSION))
            if schema_version == SCHEMA_VERSION and "application_history" not in raw:
                raise ValueError("application_history is required for schema_version 2")
            history_raw = raw.get("application_history", ())
            if not isinstance(history_raw, (list, tuple)):
                raise ValueError("application_history must be a list")
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
                application_history=tuple(
                    ApplicationEvent.from_dict(item) for item in history_raw
                ),
                migration_source=_optional_string(raw.get("migration_source")),
                screening_provider=_optional_string(raw.get("screening_provider")),
                verdict_capped=_strict_bool(raw.get("verdict_capped", False)),
                prescreen_reason=_optional_string(raw.get("prescreen_reason")),
                schema_version=schema_version,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Invalid job record: {exc}") from exc


def _strict_bool(value: Any) -> bool:
    # Not bool(): the JSON string "false" is truthy, and a record claiming a cap it
    # does not have would be picked up and republished by `queue capped --rescreen`.
    if not isinstance(value, bool):
        raise ValueError(f"expected boolean, got {type(value).__name__}")
    return value


def _schema_version(value: Any) -> int:
    if type(value) is not int:
        raise ValueError(f"expected integer schema_version, got {type(value).__name__}")
    return value


def _required_string(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"expected string for {field}, got {type(value).__name__}")
    return value


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"expected string or null, got {type(value).__name__}")
    return value


def _normalize_note(value: str | None) -> str | None:
    if value is None:
        return None
    note = value.strip()
    if not note:
        return None
    if len(note) > _MAX_APPLICATION_NOTE_LENGTH:
        raise ValueError(
            f"note must be {_MAX_APPLICATION_NOTE_LENGTH} characters or fewer"
        )
    return note


def _parse_iso8601(value: str) -> None:
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid ISO 8601 timestamp: {value}") from exc


def _synthesize_legacy_event(
    status: ApplicationStatus,
    occurred_at: str | None,
) -> ApplicationEvent | None:
    if occurred_at is None:
        return None
    try:
        _parse_iso8601(occurred_at)
    except ValueError:
        return None
    return ApplicationEvent(status=status, occurred_at=occurred_at, note=None)
