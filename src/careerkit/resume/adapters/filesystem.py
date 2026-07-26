from __future__ import annotations

import glob
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from careerkit.workspace import WorkspacePaths


@dataclass
class ResumeWorkspaceAdapter:
    base_dir: Path
    workspace: WorkspacePaths | None = None
    target: str | None = None

    @property
    def profile_dir(self) -> Path:
        return self.base_dir / "profile"

    @property
    def companies_dir(self) -> Path:
        return self.base_dir / "companies"

    @property
    def overrides_dir(self) -> Path:
        return self.base_dir / "overrides"

    @property
    def build_dir(self) -> Path:
        return self.base_dir / "build"

    @property
    def variant_config_path(self) -> Path:
        return self.base_dir / "variant_config.json"

    @property
    def verify_content_config_path(self) -> Path:
        return self.base_dir / "verify_content_config.json"

    def load_json(self, path: Path) -> dict[str, Any]:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def load_variant_config(self) -> dict[str, Any]:
        if not self.variant_config_path.is_file():
            raise ValueError(
                "variant_config.json is missing; copy variant_config.example.json "
                "to private/variant_config.json"
            )
        return self.load_json(self.variant_config_path)

    def load_verify_content_config(self, path: Path | None = None) -> dict[str, Any]:
        config_path = path or self.verify_content_config_path
        if not config_path.is_file():
            raise ValueError(
                "verify_content_config.json is missing; copy verify_content_config.example.json "
                "to private/verify_content_config.json"
            )
        return self.load_json(config_path)

    def load_target_config(self, target: str | None, variant: str) -> dict[str, Any]:
        config = self.load_variant_config()
        base_config = dict(config.get(variant, config["job"]))
        if not target:
            return base_config
        config_path = self.overrides_dir / target / "config.json"
        if not config_path.exists():
            return base_config
        try:
            target_config = self.load_json(config_path)
        except (OSError, json.JSONDecodeError):
            return base_config
        variant_overrides = target_config.get(variant, {})
        if "company_detail" in variant_overrides:
            base_config["company_detail"] = {
                **base_config.get("company_detail", {}),
                **variant_overrides["company_detail"],
            }
        for key in (
            "companies",
            "include_certificates",
            "include_awards",
            "include_languages",
            "include_open_source",
        ):
            if key in variant_overrides:
                base_config[key] = variant_overrides[key]
        return base_config

    def resolve_path(self, base_path: Path, target: str | None = None) -> Path:
        resolved_target = target if target is not None else self.target
        if not resolved_target or not base_path.exists():
            return base_path
        try:
            relpath = base_path.relative_to(self.base_dir)
        except ValueError:
            return base_path
        override = self.overrides_dir / resolved_target / relpath
        return override if override.exists() else base_path

    def resolve_glob(self, base_dir: Path, pattern: str, target: str | None = None) -> list[str]:
        resolved_target = target if target is not None else self.target
        if resolved_target:
            try:
                relpath = base_dir.relative_to(self.base_dir)
            except ValueError:
                relpath = None
            if relpath is not None:
                override_dir = self.overrides_dir / resolved_target / relpath
                if override_dir.is_dir():
                    override_files = sorted(glob.glob(str(override_dir / pattern)))
                    if override_files:
                        return override_files
        return sorted(glob.glob(str(base_dir / pattern)))

    def read_file(self, path: Path, target: str | None = None) -> str:
        resolved = self.resolve_path(path, target)
        return resolved.read_text(encoding="utf-8")
