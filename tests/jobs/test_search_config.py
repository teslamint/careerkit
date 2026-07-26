from __future__ import annotations

import stat
from pathlib import Path

import yaml

from careerkit.jobs.application.config import (
    ConfigValidationError,
    SearchConfigService,
    load_runtime_config,
)
from careerkit.jobs.adapters.config_files import YamlConfigFileAdapter


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


def test_preview_converts_exact_legacy_backend_mappings_without_writing(tmp_path: Path) -> None:
    config_path = tmp_path / "search_config.yaml"
    config_path.write_text(yaml.safe_dump(LEGACY_RAW, allow_unicode=True, sort_keys=False), encoding="utf-8")
    service = SearchConfigService(YamlConfigFileAdapter(config_path))

    preview = service.preview()

    assert preview.ready is False
    assert preview.action == "apply"
    assert preview.normalized_role == "backend"
    assert preview.would_write is False
    assert preview.converted_config["search"]["role"] == "backend"
    assert "job_ids" not in preview.converted_config["platforms"]["wanted"]
    assert "job_category_names" not in preview.converted_config["platforms"]["remember"]
    assert "position_types" not in preview.converted_config["platforms"]["groupby"]
    assert config_path.read_text(encoding="utf-8") == yaml.safe_dump(LEGACY_RAW, allow_unicode=True, sort_keys=False)


def test_apply_rewrites_atomically_with_backup_and_is_idempotent(tmp_path: Path) -> None:
    config_path = tmp_path / "search_config.yaml"
    config_path.write_text(yaml.safe_dump(LEGACY_RAW, allow_unicode=True, sort_keys=False), encoding="utf-8")
    service = SearchConfigService(YamlConfigFileAdapter(config_path))

    applied = service.apply()

    assert applied.action == "apply"
    assert applied.changed is True
    assert applied.backup_path is not None and applied.backup_path.exists()
    assert stat.S_IMODE(applied.backup_path.stat().st_mode) <= 0o600
    runtime = load_runtime_config(applied.config)
    assert runtime.role == "backend"
    assert runtime.platforms["wanted"].enabled is True

    second = service.apply()
    assert second.action == "noop"
    assert second.changed is False


def test_conflicting_normalized_and_native_values_are_rejected(tmp_path: Path) -> None:
    raw = {
        "search": {"role": "backend"},
        "platforms": {
            "wanted": {"enabled": True, "job_group_id": 518, "job_ids": [999]},
        },
    }
    config_path = tmp_path / "search_config.yaml"
    config_path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    service = SearchConfigService(YamlConfigFileAdapter(config_path))

    preview = service.preview()

    assert preview.action == "reject"
    assert any(item.code == "conflicting_native_role_mapping" for item in preview.diagnostics)


def test_runtime_load_rejects_removed_native_keys_even_if_exact() -> None:
    try:
        load_runtime_config(LEGACY_RAW)
    except ConfigValidationError as exc:
        assert "removed raw role key" in str(exc)
    else:
        raise AssertionError("expected ConfigValidationError")


def test_tracked_example_config_is_ready_for_normalized_runtime() -> None:
    example_path = Path(__file__).resolve().parents[2] / "search_config.example.yaml"
    service = SearchConfigService(YamlConfigFileAdapter(example_path))
    example_text = example_path.read_text(encoding="utf-8")

    check = service.check()
    runtime = load_runtime_config(service.adapter.read())

    assert check.ready is True
    assert check.action == "noop"
    assert check.normalized_role == "backend"
    assert check.findings == ()
    assert runtime.role == "backend"
    for removed_key in ("job_group_id", "job_ids", "job_category_names", "position_types"):
        assert removed_key not in example_text


def test_runtime_defaults_to_wanted_when_no_platform_is_explicitly_enabled() -> None:
    minimal = load_runtime_config({"search": {"role": "backend"}})
    omitted_enabled = load_runtime_config(
        {
            "search": {"role": "backend"},
            "platforms": {"remember": {"base_url": "https://remember.example"}},
        }
    )

    assert minimal.platforms["wanted"].enabled is True
    assert omitted_enabled.platforms["wanted"].enabled is True
    assert omitted_enabled.platforms["remember"].enabled is False


def test_runtime_preserves_explicitly_disabled_platforms() -> None:
    runtime = load_runtime_config(
        {
            "search": {"role": "backend"},
            "platforms": {
                "wanted": {"enabled": False},
                "remember": {"enabled": False},
            },
        }
    )

    assert not any(platform.enabled for platform in runtime.platforms.values())


def test_config_check_rejects_null_mapping_sections(tmp_path: Path) -> None:
    config_path = tmp_path / "search_config.yaml"
    config_path.write_text("search:\nplatforms:\n", encoding="utf-8")
    result = SearchConfigService(YamlConfigFileAdapter(config_path)).check()
    assert result.ready is False
    assert result.findings[0].code == "invalid_search_section"


def test_config_check_rejects_null_optional_mapping(tmp_path: Path) -> None:
    config_path = tmp_path / "search_config.yaml"
    config_path.write_text("search:\n  role: backend\nquick_filters:\n", encoding="utf-8")
    result = SearchConfigService(YamlConfigFileAdapter(config_path)).check()
    assert result.ready is False
    assert result.findings[0].code == "invalid_quick_filters_section"
