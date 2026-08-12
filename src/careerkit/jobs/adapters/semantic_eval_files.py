from __future__ import annotations

import errno
import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import time
from typing import Mapping, Sequence, cast

# A publish writes one small JSON document, so a temp file this fresh belongs to a
# live publish rather than to a crashed one. Reaping waits until well past that.
ORPHAN_MIN_AGE_SECONDS = 300.0


class SemanticEvalFileStore:
    def __init__(self, *, allowed_roots: Sequence[Path]) -> None:
        if not allowed_roots:
            raise ValueError('at least one allowed root is required')
        self.allowed_roots = tuple(root.expanduser().resolve(strict=False) for root in allowed_roots)
        self._uid = os.getuid()
        self._nofollow = getattr(os, 'O_NOFOLLOW', 0)
        self._directory = getattr(os, 'O_DIRECTORY', 0)

    def read_json(self, path: Path, *, purpose: str) -> Mapping[str, object]:
        normalized = self._normalize_path(path)
        parent_fd, _parent_path, leaf_name = self._open_parent_dir(normalized, create_missing=False)
        try:
            descriptor = os.open(leaf_name, os.O_RDONLY | self._nofollow, dir_fd=parent_fd)
            try:
                metadata = os.fstat(descriptor)
                self._assert_private_file(metadata, purpose)
                with os.fdopen(descriptor, 'r', encoding='utf-8') as handle:
                    descriptor = -1
                    payload = json.load(handle)
            except Exception:
                if descriptor >= 0:
                    os.close(descriptor)
                raise
        finally:
            os.close(parent_fd)
        if not isinstance(payload, Mapping):
            raise ValueError(f'{purpose} must be a JSON object')
        return cast(Mapping[str, object], payload)

    def write_new_json(self, path: Path, payload: Mapping[str, object], *, purpose: str) -> Path:
        normalized = self._normalize_path(path)
        self._reject_tracked_output(normalized)
        parent_fd, parent_path, leaf_name = self._open_parent_dir(normalized, create_missing=True)
        temp_path: Path | None = None
        descriptor = -1
        try:
            self._cleanup_orphans(parent_path, leaf_name)
            serialized = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            descriptor, temp_name = tempfile.mkstemp(
                dir=parent_path,
                prefix=f'.{leaf_name}.tmp-',
            )
            temp_path = Path(temp_name)
            os.fchmod(descriptor, 0o600)
            # Held until the descriptor closes, so a concurrent publish sees this
            # temp file as in use instead of collecting it as an orphan.
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.write(descriptor, serialized.encode('utf-8'))
            os.fsync(descriptor)
            try:
                os.link(temp_path.name, leaf_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            except FileExistsError:
                raise FileExistsError(f'{purpose} target already exists: {leaf_name}') from None
            self._fsync_directory(parent_fd)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            os.close(parent_fd)
        return normalized

    def _normalize_path(self, path: Path) -> Path:
        candidate = path.expanduser()
        if not candidate.is_absolute():
            candidate = self.allowed_roots[0] / candidate
        normalized = Path(os.path.normpath(str(candidate)))
        for root in self.allowed_roots:
            try:
                normalized.relative_to(root)
                return normalized
            except ValueError:
                continue
        raise ValueError('semantic eval path must stay inside an allowed root')

    def _open_parent_dir(self, path: Path, *, create_missing: bool) -> tuple[int, Path, str]:
        root = self._match_root(path)
        relative_parts = path.relative_to(root).parts
        if not relative_parts:
            raise ValueError('semantic eval path must include a file name')
        root_fd = os.open(root, os.O_RDONLY | self._directory | self._nofollow)
        current_fd = root_fd
        current_path = root
        try:
            self._assert_private_directory(os.fstat(current_fd), str(root))
            for part in relative_parts[:-1]:
                if part in {'', '.', '..'}:
                    raise ValueError('semantic eval path must not escape the allowed root')
                try:
                    next_fd = os.open(part, os.O_RDONLY | self._directory | self._nofollow, dir_fd=current_fd)
                except OSError as exc:
                    if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                        raise ValueError(f'semantic eval path must not traverse a symlink: {part}') from exc
                    if not isinstance(exc, FileNotFoundError):
                        raise
                    if not create_missing:
                        raise exc
                    os.mkdir(part, 0o700, dir_fd=current_fd)
                    next_fd = os.open(part, os.O_RDONLY | self._directory | self._nofollow, dir_fd=current_fd)
                self._assert_private_directory(os.fstat(next_fd), part)
                os.close(current_fd)
                current_fd = next_fd
                current_path = current_path / part
            return current_fd, current_path, relative_parts[-1]
        except Exception:
            os.close(current_fd)
            raise

    def _match_root(self, path: Path) -> Path:
        for root in sorted(self.allowed_roots, key=lambda item: len(str(item)), reverse=True):
            try:
                path.relative_to(root)
                return root
            except ValueError:
                continue
        raise ValueError('semantic eval path must stay inside an allowed root')

    def _reject_tracked_output(self, path: Path) -> None:
        repo_root = self._find_git_root(path.parent)
        if repo_root is None:
            return
        try:
            relative = path.relative_to(repo_root)
        except ValueError:
            return
        result = subprocess.run(
            ['git', '-C', str(repo_root), 'ls-files', '--error-unmatch', '--', str(relative)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode == 0:
            raise ValueError('semantic eval output must not target a tracked git path')

    @staticmethod
    def _find_git_root(path: Path) -> Path | None:
        for candidate in (path, *path.parents):
            if (candidate / '.git').exists():
                return candidate
        return None

    def _cleanup_orphans(self, parent_path: Path, leaf_name: str) -> None:
        prefix = f'.{leaf_name}.tmp-'
        cutoff = time.time() - ORPHAN_MIN_AGE_SECONDS
        for candidate in parent_path.iterdir():
            if not candidate.name.startswith(prefix):
                continue
            try:
                metadata = candidate.lstat()
            except FileNotFoundError:
                continue
            if metadata.st_uid != self._uid or not stat.S_ISREG(metadata.st_mode):
                continue
            if metadata.st_mode & 0o077:
                continue
            if metadata.st_mtime > cutoff:
                continue
            if self._is_locked(candidate):
                continue
            candidate.unlink(missing_ok=True)

    def _is_locked(self, candidate: Path) -> bool:
        try:
            descriptor = os.open(candidate, os.O_RDONLY | self._nofollow)
        except FileNotFoundError:
            return False
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                return True
            raise
        finally:
            os.close(descriptor)
        return False

    def _assert_private_directory(self, metadata: os.stat_result, label: str) -> None:
        if metadata.st_uid != self._uid:
            raise ValueError(f'semantic eval directory must be owned by the current user: {label}')
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f'semantic eval path component must be a directory: {label}')
        if metadata.st_mode & 0o077:
            raise ValueError(f'semantic eval directory must be owner-only: {label}')

    def _assert_private_file(self, metadata: os.stat_result, purpose: str) -> None:
        if metadata.st_uid != self._uid:
            raise ValueError(f'{purpose} must be owned by the current user')
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f'{purpose} must be a regular file')
        if metadata.st_mode & 0o077:
            raise ValueError(f'{purpose} must be mode 0600')

    @staticmethod
    def _fsync_directory(descriptor: int) -> None:
        os.fsync(descriptor)
