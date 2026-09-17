"""Bounded Git observations for private portfolio research."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import os
from pathlib import Path
import selectors
import subprocess
import time
from collections.abc import Mapping, Sequence
import re
from urllib.parse import urlsplit

from careerkit.resume.domain.portfolio_evidence import LogicalRepository, RawRepositoryOccurrence


@dataclass(frozen=True, slots=True)
class RepositoryObservation:
    occurrence_id: str
    context_id: str
    git_common_dir: Path
    object_store_id: str
    remote_identity: str | None
    observed_refs: tuple[str, ...]
    reachable_commits: tuple[str, ...]
    period_ids: tuple[str, ...] = ("period-1",)


@dataclass(frozen=True, slots=True)
class CommitObservation:
    commit_id: str
    author_name: str
    author_email: str
    authored_at: datetime
    parent_ids: tuple[str, ...]

    @property
    def parent_count(self) -> int:
        return len(self.parent_ids)


class GitPortfolioIssue(RuntimeError):
    def __init__(self, code: str, message: str, *, blocking: bool = True):
        super().__init__(message)
        self.code = code
        self.blocking = blocking


class GitPortfolioInspector:
    """Discover and inspect Git repositories without shell interpolation."""

    def __init__(
        self,
        *,
        context_id: str | None = None,
        period_ids: tuple[str, ...] = ("period-1",),
        context_roots: Mapping[Path, tuple[str, tuple[str, ...]]] | None = None,
        timeout: float = 30,
        max_output_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        if context_id is None and not context_roots:
            raise ValueError("context_id or context_roots is required")
        self.context_id = context_id
        self.period_ids = period_ids
        self.context_roots = {
            root.resolve(): value for root, value in (context_roots or {}).items()
        }
        self.timeout = timeout
        self.max_output_bytes = max_output_bytes
        self._occurrences: dict[str, RawRepositoryOccurrence] = {}

    def discover(self, root: Path) -> list[RawRepositoryOccurrence]:
        root = root.resolve()
        candidates: list[tuple[Path, str]] = []
        for directory, names, _files in os.walk(root):
            current = Path(directory)
            names[:] = [name for name in names if name != ".git"]
            dot_git = current / ".git"
            if dot_git.is_dir():
                candidates.append((current, "standard"))
            elif dot_git.is_file():
                pointer = dot_git.read_text(encoding="utf-8", errors="replace")
                repository_type = "submodule" if "/modules/" in pointer.replace("\\", "/") else "worktree"
                candidates.append((current, repository_type))
            elif (current / "HEAD").is_file() and (current / "objects").is_dir() and (current / "refs").is_dir():
                candidates.append((current, "bare"))
                names[:] = []

        unresolved: list[RawRepositoryOccurrence] = []
        resolved: list[tuple[Path, str, RepositoryObservation]] = []
        for path, repository_type in sorted(set(candidates), key=lambda item: str(item[0])):
            occurrence_id = _safe_id("occurrence", str(path))
            if _is_broken_git_pointer(path):
                unresolved.append(
                    RawRepositoryOccurrence(
                        occurrence_id,
                        path,
                        repository_type,
                        None,
                        None,
                        (),
                        None,
                        "broken-git-pointer",
                        "unresolved",
                    )
                )
                continue
            context_id, period_ids = self._context_for_path(path)
            observation = self._observe_path(occurrence_id, path, context_id, period_ids)
            resolved.append((path, repository_type, observation))

        observations = [item[2] for item in resolved]
        occurrences = list(unresolved)
        for path, repository_type, observation in resolved:
            logical_id = _logical_id(observation, observations)
            occurrence = RawRepositoryOccurrence(
                observation.occurrence_id,
                path,
                repository_type,
                observation.git_common_dir,
                observation.object_store_id,
                observation.observed_refs,
                logical_id,
                None,
                "resolved",
            )
            occurrences.append(occurrence)
            self._occurrences[occurrence.occurrence_id] = occurrence
        for occurrence in unresolved:
            self._occurrences[occurrence.occurrence_id] = occurrence
        return sorted(occurrences, key=lambda item: str(item.local_path))

    def observe(self, occurrence: RawRepositoryOccurrence) -> RepositoryObservation:
        if occurrence.resolution != "resolved":
            raise GitPortfolioIssue("unresolved", "repository occurrence is unresolved")
        context_id, period_ids = self._context_for_path(occurrence.local_path)
        return self._observe_path(occurrence.occurrence_id, occurrence.local_path, context_id, period_ids)

    def commits(self, logical: LogicalRepository) -> list[CommitObservation]:
        occurrences = [
            self._occurrences[item]
            for item in logical.occurrence_ids
            if item in self._occurrences
        ]
        if len(occurrences) != len(logical.occurrence_ids):
            raise GitPortfolioIssue("missing-occurrence", "logical repository has an unobserved occurrence")
        commits_by_id: dict[str, CommitObservation] = {}
        for occurrence in occurrences:
            for commit in self._commits_for_occurrence(occurrence):
                previous = commits_by_id.get(commit.commit_id)
                if previous is not None and previous != commit:
                    raise GitPortfolioIssue("malformed", "Git commit metadata differs across occurrences")
                commits_by_id[commit.commit_id] = commit
        return sorted(commits_by_id.values(), key=lambda item: item.commit_id)

    def _commits_for_occurrence(
        self,
        occurrence: RawRepositoryOccurrence,
    ) -> list[CommitObservation]:
        try:
            output = self._checked_output(
                self._run(
                    occurrence.local_path,
                    "log",
                    "HEAD",
                    "--branches",
                    "--remotes",
                    "--tags",
                    "--format=%H%x1f%an%x1f%ae%x1f%aI%x1f%P%x1e",
                )
            )
        except subprocess.TimeoutExpired as exc:
            raise GitPortfolioIssue("timeout", "Git commit enumeration exceeded its timeout") from exc
        commits: list[CommitObservation] = []
        for raw_record in output.split("\x1e"):
            record = raw_record.strip("\r\n")
            if not record:
                continue
            fields = record.split("\x1f")
            if len(fields) != 5:
                raise GitPortfolioIssue("malformed", "Git commit output has an invalid field count")
            commit_id, author_name, author_email, authored_at, parents = fields
            if not _is_object_id(commit_id):
                raise GitPortfolioIssue("malformed", "Git commit output has an invalid object ID")
            try:
                timestamp = datetime.fromisoformat(authored_at)
            except ValueError as exc:
                raise GitPortfolioIssue("malformed", "Git commit output has an invalid timestamp") from exc
            commits.append(
                CommitObservation(
                    commit_id,
                    author_name,
                    author_email,
                    timestamp,
                    tuple(parent for parent in parents.split() if parent),
                )
            )
        return commits

    def _observe_path(
        self,
        occurrence_id: str,
        path: Path,
        context_id: str,
        period_ids: tuple[str, ...],
    ) -> RepositoryObservation:
        try:
            common_text = self._checked_output(
                self._run(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
            ).strip()
            if not common_text or "\n" in common_text:
                raise GitPortfolioIssue("malformed", "Git common directory output is malformed")
            common_dir = Path(common_text)
            head = self._checked_output(self._run(path, "rev-parse", "HEAD")).strip()
            if not _is_object_id(head):
                raise GitPortfolioIssue("malformed", "Git HEAD output is malformed")
            refs_output = self._checked_output(
                self._run(
                    path,
                    "for-each-ref",
                    "--format=%(refname)=%(objectname)",
                    "refs/heads",
                    "refs/remotes",
                    "refs/tags",
                )
            )
            refs = tuple(sorted({line for line in refs_output.splitlines() if line}))
            if any(not _valid_ref(line) for line in refs):
                raise GitPortfolioIssue("malformed", "Git ref output is malformed")
            commits_output = self._checked_output(
                self._run(path, "rev-list", "HEAD", "--branches", "--remotes", "--tags")
            )
            commits = tuple(sorted({line for line in commits_output.splitlines() if line}))
            if any(not _is_object_id(item) for item in commits):
                raise GitPortfolioIssue("malformed", "Git reachable commit output is malformed")
            remote_output = self._checked_output(
                self._run(path, "config", "--get", "remote.origin.url", allow_failure=True)
            ).strip()
        except subprocess.TimeoutExpired as exc:
            raise GitPortfolioIssue("timeout", "Git observation exceeded its timeout") from exc
        refs_with_head = tuple(sorted({f"HEAD={head}", *refs}))
        return RepositoryObservation(
            occurrence_id,
            context_id,
            common_dir,
            sha256(str(common_dir / "objects").encode()).hexdigest(),
            _normalize_remote(remote_output) if remote_output else None,
            refs_with_head,
            commits,
            period_ids,
        )

    def _context_for_path(self, path: Path) -> tuple[str, tuple[str, ...]]:
        resolved = path.resolve()
        matches = [
            (root, value)
            for root, value in self.context_roots.items()
            if resolved.is_relative_to(root)
        ]
        if matches:
            return max(matches, key=lambda item: len(item[0].parts))[1]
        if self.context_id is None:
            raise GitPortfolioIssue("unmapped-root", "repository is outside every approved context root")
        return self.context_id, self.period_ids

    def _run(self, path: Path, *arguments: str, allow_failure: bool = False) -> str:
        command = ["git", "-C", str(path), *arguments]
        stdout, _stderr, returncode = self._run_command(command)
        if returncode != 0 and not allow_failure:
            raise GitPortfolioIssue("git-error", "Git observation failed")
        return stdout

    def _run_command(self, command: Sequence[str]) -> tuple[str, str, int]:
        process = subprocess.Popen(
            list(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if process.stdout is None or process.stderr is None:
            process.kill()
            raise GitPortfolioIssue("git-error", "subprocess pipes are unavailable")
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        chunks: dict[str, list[bytes]] = {"stdout": [], "stderr": []}
        total = 0
        deadline = time.monotonic() + self.timeout
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    process.kill()
                    process.wait()
                    raise subprocess.TimeoutExpired(list(command), self.timeout)
                events = selector.select(remaining)
                if not events:
                    continue
                for key, _mask in events:
                    stream = key.fileobj
                    data = os.read(key.fd, 65536)
                    if not data:
                        selector.unregister(stream)
                        continue
                    total += len(data)
                    if total > self.max_output_bytes:
                        process.kill()
                        process.wait()
                        raise GitPortfolioIssue("overflow", "Git output exceeded the configured bound")
                    chunks[key.data].append(data)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill()
                process.wait()
                raise subprocess.TimeoutExpired(list(command), self.timeout)
            returncode = process.wait(timeout=remaining)
        finally:
            selector.close()
            if process.poll() is None:
                process.kill()
                process.wait()
        try:
            stdout = b"".join(chunks["stdout"]).decode("utf-8")
            stderr = b"".join(chunks["stderr"]).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise GitPortfolioIssue("malformed", "Git output is not UTF-8") from exc
        return stdout, stderr, returncode

    def _checked_output(self, output: str) -> str:
        if len(output.encode()) > self.max_output_bytes:
            raise GitPortfolioIssue("overflow", "Git output exceeded the configured bound")
        return output


def _logical_id(
    observation: RepositoryObservation,
    observations: list[RepositoryObservation],
) -> str:
    return _safe_id("logical", "|".join(repository_group_key(observation, observations)))


def repository_group_key(
    observation: RepositoryObservation,
    observations: Sequence[RepositoryObservation],
) -> tuple[str, ...]:
    same_common = [
        item
        for item in observations
        if item.context_id == observation.context_id
        and item.git_common_dir == observation.git_common_dir
    ]
    if observation.remote_identity is None or len(same_common) > 1:
        return ("common", observation.context_id, str(observation.git_common_dir))
    return (
        "clone",
        observation.context_id,
        observation.remote_identity,
        *observation.reachable_commits,
    )


def _safe_id(prefix: str, value: str) -> str:
    return f"{prefix}-{sha256(value.encode()).hexdigest()[:16]}"


def _normalize_remote(value: str) -> str:
    raw = value.strip()
    if "://" in raw:
        parsed = urlsplit(raw)
        host = (parsed.hostname or "").lower()
        path = parsed.path.strip("/").removesuffix(".git")
        return f"{host}/{path}" if host else path
    scp = re.fullmatch(r"(?:[^@/]+@)?([^:/]+):(.+)", raw)
    if scp:
        host, path = scp.groups()
        return f"{host.lower()}/{path.strip('/').removesuffix('.git')}"
    return raw.removesuffix("/").removesuffix(".git")


def _is_broken_git_pointer(path: Path) -> bool:
    pointer = path / ".git"
    if not pointer.is_file():
        return False
    text = pointer.read_text(encoding="utf-8", errors="strict").strip()
    if not text.startswith("gitdir: "):
        return True
    target = Path(text.removeprefix("gitdir: "))
    if not target.is_absolute():
        target = path / target
    return not target.resolve().is_dir()


def _is_object_id(value: str) -> bool:
    return 40 <= len(value) <= 64 and all(character in "0123456789abcdef" for character in value)


def _valid_ref(value: str) -> bool:
    if "=" not in value:
        return False
    name, object_id = value.rsplit("=", 1)
    return name.startswith(("refs/heads/", "refs/remotes/", "refs/tags/")) and _is_object_id(object_id)
