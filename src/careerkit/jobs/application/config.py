from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol
import copy

from careerkit.jobs.adapters.http import UrllibHttpClient
from careerkit.jobs.adapters.platforms.groupby import GROUPBY_BACKEND_MAPPING
from careerkit.jobs.adapters.platforms.remember import REMEMBER_BACKEND_MAPPING
from careerkit.jobs.adapters.platforms.wanted import WANTED_BACKEND_MAPPING
from careerkit.jobs.application.title_filter import normalize_job_queries


class ConfigValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ConfigDiagnostic:
    code: str
    message: str


@dataclass(frozen=True)
class PlatformRuntimeConfig:
    enabled: bool
    base_url: str


@dataclass(frozen=True)
class SearchConfig:
    role: str
    platforms: dict[str, PlatformRuntimeConfig]
    search_queries: tuple[str, ...]
    execution: dict[str, Any]
    quick_filters: dict[str, Any]
    filters: dict[str, Any]
    rate_limits: dict[str, Any]
    semantic_filter: dict[str, Any]
    rejected_companies: set[str] = field(default_factory=set)
    http_client: UrllibHttpClient = field(default_factory=UrllibHttpClient)

    @property
    def max_urls_per_run(self) -> int:
        return int(self.execution.get("max_urls_per_run", 50))

    @property
    def semantic_enabled(self) -> bool:
        return bool(self.semantic_filter.get("enabled", False))


@dataclass(frozen=True)
class ConfigPreviewResult:
    ready: bool
    action: str
    normalized_role: str | None
    diagnostics: tuple[ConfigDiagnostic, ...]
    converted_config: dict[str, Any]
    would_write: bool


@dataclass(frozen=True)
class ConfigApplyResult:
    action: str
    changed: bool
    diagnostics: tuple[ConfigDiagnostic, ...]
    config: dict[str, Any]
    backup_path: Any = None


@dataclass(frozen=True)
class ConfigCheckResult:
    ready: bool
    action: str
    normalized_role: str | None
    findings: tuple[ConfigDiagnostic, ...]


class ConfigAdapter(Protocol):
    def read(self) -> dict[str, Any]: ...
    def write(self, raw: Mapping[str, Any]) -> Any: ...
    def backup(self) -> Any: ...
    def restore(self, backup_path: Any) -> Any: ...


_BACKEND_NATIVE_KEYS = {
    "wanted": WANTED_BACKEND_MAPPING,
    "remember": REMEMBER_BACKEND_MAPPING,
    "groupby": GROUPBY_BACKEND_MAPPING,
}
_BASE_URLS = {
    "wanted": "https://www.wanted.co.kr",
    "remember": "https://career.rememberapp.co.kr",
    "groupby": "https://groupby.kr",
    "saramin": "https://www.saramin.co.kr",
    "thevc": "https://thevc.kr",
}


def _inspect(raw: Mapping[str, Any]) -> tuple[list[ConfigDiagnostic], dict[str, Any], str]:
    diagnostics: list[ConfigDiagnostic] = []
    converted = copy.deepcopy(dict(raw))
    search_section = converted.get("search", {})
    if not isinstance(search_section, dict):
        diagnostics.append(ConfigDiagnostic("invalid_search_section", "search must be a mapping"))
        return diagnostics, converted, "reject"
    converted["search"] = search_section
    role = search_section.get("role")
    platforms = converted.get("platforms", {})
    if not isinstance(platforms, dict):
        diagnostics.append(ConfigDiagnostic("invalid_platforms_section", "platforms must be a mapping"))
        return diagnostics, converted, "reject"
    converted["platforms"] = platforms
    native_present = False
    exact_native = True
    conflicting = False
    for platform_name, expected in _BACKEND_NATIVE_KEYS.items():
        section = platforms.get(platform_name)
        if not isinstance(section, dict):
            continue
        observed = {key: section.get(key) for key in expected if key in section}
        if not observed:
            continue
        native_present = True
        if observed != expected:
            exact_native = False
            conflicting = True
            diagnostics.append(ConfigDiagnostic("conflicting_native_role_mapping", f"{platform_name} removed raw role key does not match backend mapping"))
            continue
        for key in expected:
            section.pop(key, None)
    if role is None:
        if native_present and exact_native:
            search_section["role"] = "backend"
            diagnostics.append(ConfigDiagnostic("legacy_native_role_mapping", "removed raw role key can be converted to search.role=backend"))
            return diagnostics, converted, "apply"
        diagnostics.append(ConfigDiagnostic("missing_normalized_role", "set search.role to backend"))
        return diagnostics, converted, "reject"
    if role != "backend":
        diagnostics.append(ConfigDiagnostic("unsupported_role", "search.role must be backend"))
        return diagnostics, converted, "reject"
    if conflicting:
        return diagnostics, converted, "reject"
    if native_present:
        diagnostics.append(ConfigDiagnostic("removed_raw_role_key", "removed raw role key must be deleted before runtime search"))
        return diagnostics, converted, "apply"
    for section_name in (
        "execution",
        "quick_filters",
        "filters",
        "rate_limits",
        "semantic_filter",
    ):
        section = converted.get(section_name, {})
        if not isinstance(section, dict):
            diagnostics.append(
                ConfigDiagnostic(
                    f"invalid_{section_name}_section",
                    f"{section_name} must be a mapping",
                )
            )
            return diagnostics, converted, "reject"
    return diagnostics, converted, "noop"


class SearchConfigService:
    def __init__(self, adapter: ConfigAdapter):
        self.adapter = adapter

    def preview(self) -> ConfigPreviewResult:
        raw = self.adapter.read()
        diagnostics, converted, action = _inspect(raw)
        search = converted.get("search")
        return ConfigPreviewResult(
            ready=action == "noop",
            action=action,
            normalized_role=search.get("role") if isinstance(search, Mapping) else None,
            diagnostics=tuple(diagnostics),
            converted_config=converted,
            would_write=False,
        )

    def check(self) -> ConfigCheckResult:
        preview = self.preview()
        return ConfigCheckResult(
            ready=preview.action == "noop",
            action=preview.action,
            normalized_role=preview.normalized_role,
            findings=preview.diagnostics,
        )

    def apply(self) -> ConfigApplyResult:
        raw = self.adapter.read()
        diagnostics, converted, action = _inspect(raw)
        if action == "reject":
            return ConfigApplyResult(action=action, changed=False, diagnostics=tuple(diagnostics), config=converted)
        if action == "noop":
            return ConfigApplyResult(action=action, changed=False, diagnostics=tuple(diagnostics), config=converted)
        write_result = self.adapter.write(converted)
        return ConfigApplyResult(action="apply", changed=True, diagnostics=tuple(diagnostics), config=converted, backup_path=getattr(write_result, "backup_path", None))

    def backup(self) -> Any:
        return self.adapter.backup()

    def restore(self, backup_path: Any) -> Any:
        return self.adapter.restore(backup_path)


def load_runtime_config(raw: Mapping[str, Any]) -> SearchConfig:
    diagnostics, converted, action = _inspect(raw)
    if action != "noop":
        message = diagnostics[0].message if diagnostics else "invalid search configuration"
        raise ConfigValidationError(message if action == "reject" else f"removed raw role key: {message}")
    platforms_raw = converted.get("platforms", {})
    platforms = {
        name: PlatformRuntimeConfig(
            enabled=bool((platforms_raw.get(name) or {}).get("enabled", False)),
            base_url=str((platforms_raw.get(name) or {}).get("base_url", _BASE_URLS[name])),
        )
        for name in _BASE_URLS
    }
    enabled_declared = any(
        isinstance(section, Mapping) and "enabled" in section
        for section in platforms_raw.values()
    )
    if not enabled_declared and not any(platform.enabled for platform in platforms.values()):
        platforms["wanted"] = PlatformRuntimeConfig(
            enabled=True,
            base_url=platforms["wanted"].base_url,
        )
    queries = tuple(normalize_job_queries(list(converted.get("search_queries", ["백엔드"]))))
    return SearchConfig(
        role="backend",
        platforms=platforms,
        search_queries=queries,
        execution=dict(converted.get("execution", {})),
        quick_filters=dict(converted.get("quick_filters", {})),
        filters=dict(converted.get("filters", {})),
        rate_limits=dict(converted.get("rate_limits", {})),
        semantic_filter=dict(converted.get("semantic_filter", {})),
    )
