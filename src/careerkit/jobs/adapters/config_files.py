from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Mapping, Any

import yaml


@dataclass(frozen=True)
class WriteResult:
    path: Path
    backup_path: Path | None


class YamlConfigFileAdapter:
    def __init__(self, path: Path):
        self.path = path

    def read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        self._assert_regular_target(self.path)
        with self.path.open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle) or {}

    def backup(self) -> Path:
        if not self.path.exists():
            raise FileNotFoundError(self.path)
        self._assert_regular_target(self.path)
        original_mode = self.path.stat().st_mode & 0o777
        backup_path = self.path.with_name(f"{self.path.name}.bak")
        metadata_path = self._backup_metadata_path()
        if backup_path.exists() or backup_path.is_symlink() or metadata_path.exists() or metadata_path.is_symlink():
            raise FileExistsError(f"rollback backup already exists: {backup_path.name}")
        backup_path.write_bytes(self.path.read_bytes())
        os.chmod(backup_path, min(original_mode, 0o600))
        with backup_path.open("rb") as handle:
            os.fsync(handle.fileno())
        metadata_path.write_text(json.dumps({"mode": original_mode}) + "\n", encoding="utf-8")
        os.chmod(metadata_path, 0o600)
        with metadata_path.open("rb") as handle:
            os.fsync(handle.fileno())
        self._fsync_directory()
        return backup_path

    def restore(self, backup_path: Path) -> WriteResult:
        if not backup_path.exists():
            raise FileNotFoundError(backup_path)
        expected_backup = self.path.with_name(f"{self.path.name}.bak")
        if backup_path != expected_backup:
            raise ValueError("backup must be the same-directory rollback backup")
        self._assert_regular_target(backup_path)
        metadata_path = self._backup_metadata_path()
        self._assert_regular_target(metadata_path)
        if self.path.is_symlink() or (self.path.exists() and not self.path.is_file()):
            raise ValueError("configuration target must be a regular file")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        original_mode = int(metadata["mode"])
        if original_mode < 0 or original_mode > 0o777:
            raise ValueError("rollback metadata contains an invalid mode")
        temp_path = self.path.with_name(f".{self.path.name}.restore.tmp")
        if temp_path.exists() or temp_path.is_symlink():
            raise FileExistsError(f"restore temporary file already exists: {temp_path.name}")
        temp_path.write_bytes(backup_path.read_bytes())
        os.chmod(temp_path, 0o600)
        with temp_path.open("rb") as handle:
            os.fsync(handle.fileno())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temp_path, self.path)
        os.chmod(self.path, original_mode)
        with self.path.open("rb") as handle:
            os.fsync(handle.fileno())
        self._fsync_directory()
        return WriteResult(path=self.path, backup_path=backup_path)

    def write(self, raw: Mapping[str, Any]) -> WriteResult:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink() or (self.path.exists() and not self.path.is_file()):
            raise ValueError("configuration target must be a regular file")
        rendered = yaml.safe_dump(dict(raw), allow_unicode=True, sort_keys=False)
        backup_path = None
        original_mode = 0o600
        if self.path.exists():
            original_mode = self.path.stat().st_mode & 0o777
            backup_path = self.backup()
        temp_path = self.path.with_name(f".{self.path.name}.tmp")
        if temp_path.exists() or temp_path.is_symlink():
            raise FileExistsError(f"configuration temporary file already exists: {temp_path.name}")
        with temp_path.open("x", encoding="utf-8") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, min(original_mode, 0o600))
        os.replace(temp_path, self.path)
        with self.path.open("rb") as handle:
            os.fsync(handle.fileno())
        self._fsync_directory()
        return WriteResult(path=self.path, backup_path=backup_path)

    def _fsync_directory(self) -> None:
        dir_fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    def _backup_metadata_path(self) -> Path:
        return self.path.with_name(f"{self.path.name}.bak.meta")

    @staticmethod
    def _assert_regular_target(path: Path) -> None:
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"configuration path must be a regular file: {path.name}")
