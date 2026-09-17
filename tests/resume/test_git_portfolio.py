from __future__ import annotations

from datetime import timezone
from pathlib import Path
import subprocess
import sys

import pytest

from careerkit.resume.adapters.git_portfolio import (
    GitPortfolioInspector,
    GitPortfolioIssue,
    RepositoryObservation,
    _normalize_remote,
)
from careerkit.resume.application.portfolio_research import normalize_repositories
from careerkit.resume.domain.portfolio_evidence import LogicalRepository, RawRepositoryOccurrence


UTC = timezone.utc


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repo(path: Path, *, remote: str | None = "ssh://example.test/repo.git") -> str:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "tracked.txt").write_text("one\n", encoding="utf-8")
    _git(path, "add", "tracked.txt")
    _git(
        path,
        "-c",
        "commit.gpgsign=false",
        "-c",
        "user.name=Exact Author",
        "-c",
        "user.email=exact@example.test",
        "commit",
        "-q",
        "-m",
        "initial",
    )
    if remote is not None:
        _git(path, "remote", "add", "origin", remote)
    return _git(path, "rev-parse", "HEAD")


def test_recursive_discovery_retains_nested_bare_worktree_and_broken_pointer(tmp_path: Path) -> None:
    standard = tmp_path / "standard"
    nested = tmp_path / "group" / "nested"
    bare = tmp_path / "bare.git"
    worktree = tmp_path / "worktree"
    _repo(standard)
    _repo(nested)
    subprocess.run(["git", "clone", "-q", "--bare", str(standard), str(bare)], check=True)
    _git(standard, "worktree", "add", "-q", str(worktree))
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / ".git").write_text("gitdir: ../missing\n", encoding="utf-8")

    occurrences = GitPortfolioInspector(context_id="context-1").discover(tmp_path)

    paths = {item.local_path for item in occurrences}
    assert {standard, nested, bare, worktree, broken} <= paths
    unresolved = next(item for item in occurrences if item.local_path == broken)
    assert unresolved.resolution == "unresolved"
    assert unresolved.exclusion_reason == "broken-git-pointer"


def test_recursive_discovery_retains_empty_git_marker_as_unresolved(tmp_path: Path) -> None:
    cache = tmp_path / ".uv-cache" / "sdists-v9"
    cache.mkdir(parents=True)
    (cache / ".git").write_text("", encoding="utf-8")

    occurrences = GitPortfolioInspector(context_id="context-1").discover(tmp_path)

    assert len(occurrences) == 1
    assert occurrences[0].local_path == cache
    assert occurrences[0].resolution == "unresolved"
    assert occurrences[0].exclusion_reason == "broken-git-pointer"


def test_refs_keep_detached_head_and_exclude_non_inventory_refs(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    commit_id = _repo(repo)
    _git(repo, "update-ref", "refs/stash", commit_id)
    _git(repo, "update-ref", "refs/pull/1/head", commit_id)
    _git(repo, "checkout", "-q", "--detach", commit_id)

    occurrence = GitPortfolioInspector(context_id="context-1").discover(repo.parent)[0]

    assert f"HEAD={commit_id}" in occurrence.observed_refs
    assert not any("stash" in ref or "refs/pull/" in ref for ref in occurrence.observed_refs)


def test_commits_reachable_only_from_excluded_refs_are_not_inventory(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    retained = _repo(repo)
    (repo / "tracked.txt").write_text("two\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(
        repo,
        "-c",
        "commit.gpgsign=false",
        "-c",
        "user.name=Exact Author",
        "-c",
        "user.email=exact@example.test",
        "commit",
        "-q",
        "-m",
        "excluded",
    )
    excluded = _git(repo, "rev-parse", "HEAD")
    _git(repo, "reset", "--hard", "-q", retained)
    _git(repo, "update-ref", "refs/pull/1/head", excluded)
    inspector = GitPortfolioInspector(context_id="context-1")

    occurrence = inspector.discover(tmp_path)[0]
    logical = normalize_repositories([occurrence], [inspector.observe(occurrence)])[0]

    assert logical.qualifying_commit_count == 1
    assert [item.commit_id for item in inspector.commits(logical)] == [retained]


def test_normalization_merges_same_common_dir_and_identical_clones_only() -> None:
    commit_a = "a" * 40
    commit_b = "b" * 40
    occurrences = [
        _occurrence("o1", "/one", "/common"),
        _occurrence("o2", "/two", "/common"),
        _occurrence("o3", "/clone-a", "/clone-a/.git"),
        _occurrence("o4", "/clone-b", "/clone-b/.git"),
        _occurrence("o5", "/changed", "/changed/.git"),
        _occurrence("o6", "/no-remote", "/no-remote/.git"),
    ]
    observations = [
        _observation("o1", "/common", "origin", (commit_a,)),
        _observation("o2", "/common", "origin", (commit_a,)),
        _observation("o3", "/clone-a/.git", "origin", (commit_a,)),
        _observation("o4", "/clone-b/.git", "origin", (commit_a,)),
        _observation("o5", "/changed/.git", "origin", (commit_a, commit_b)),
        _observation("o6", "/no-remote/.git", None, (commit_a,)),
    ]

    logical = normalize_repositories(occurrences, observations)

    memberships = {item.occurrence_ids for item in logical}
    assert ("o1", "o2") in memberships
    assert ("o3", "o4") in memberships
    assert ("o5",) in memberships
    assert ("o6",) in memberships
    changed = next(item for item in logical if item.occurrence_ids == ("o5",))
    assert changed.qualifying_commit_count == 2


@pytest.mark.parametrize("failure", ["timeout", "overflow", "malformed"])
def test_git_measurement_failures_are_typed_and_blocking(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    inspector = GitPortfolioInspector(
        context_id="context-1",
        max_output_bytes=64 if failure == "malformed" else 8,
    )
    occurrence = _occurrence("o1", str(tmp_path / "repo"), str(tmp_path / "repo" / ".git"))

    if failure == "timeout":
        monkeypatch.setattr(inspector, "_run", lambda *_args: (_ for _ in ()).throw(subprocess.TimeoutExpired([], 30)))
    elif failure == "overflow":
        monkeypatch.setattr(inspector, "_run", lambda *_args: "x" * 20)
    else:
        monkeypatch.setattr(inspector, "_run", lambda *_args: "not-a-valid-observation")

    with pytest.raises(GitPortfolioIssue) as caught:
        inspector.observe(occurrence)
    assert caught.value.blocking is True
    assert caught.value.code == failure


def test_commit_measurement_timeout_is_typed_and_blocking(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    _repo(repo)
    inspector = GitPortfolioInspector(context_id="context-1")
    occurrence = inspector.discover(tmp_path)[0]
    logical = normalize_repositories([occurrence], [inspector.observe(occurrence)])[0]

    def raise_timeout(*_args: object) -> str:
        raise subprocess.TimeoutExpired(["git", "log"], 30)

    monkeypatch.setattr(inspector, "_run", raise_timeout)

    with pytest.raises(GitPortfolioIssue) as caught:
        inspector.commits(logical)

    assert caught.value.blocking is True
    assert caught.value.code == "timeout"


def test_commit_output_is_parsed_with_timezone_and_parent_count(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    commit_id = _repo(repo)
    inspector = GitPortfolioInspector(context_id="context-1")
    occurrence = inspector.discover(tmp_path)[0]
    logical = normalize_repositories([occurrence], [inspector.observe(occurrence)])[0]

    commits = inspector.commits(logical)

    assert commits[0].commit_id == commit_id
    assert commits[0].author_name == "Exact Author"
    assert commits[0].authored_at.tzinfo is not None
    assert commits[0].parent_count == 0


def test_each_discovered_occurrence_remains_available_for_commit_observation(tmp_path: Path) -> None:
    _repo(tmp_path / "one", remote="ssh://example.test/one.git")
    _repo(tmp_path / "two", remote="ssh://example.test/two.git")
    inspector = GitPortfolioInspector(context_id="context-1")
    occurrences = inspector.discover(tmp_path)
    observations = [inspector.observe(item) for item in occurrences]

    logical = normalize_repositories(occurrences, observations)

    assert len(logical) == 2
    assert all(inspector.commits(item) for item in logical)


def test_commits_unions_unique_commits_from_every_logical_occurrence(tmp_path: Path) -> None:
    first_id = _repo(tmp_path / "one", remote="ssh://example.test/one.git")
    second = tmp_path / "two"
    _repo(second, remote="ssh://example.test/two.git")
    (second / "unique.txt").write_text("unique\n", encoding="utf-8")
    _git(second, "add", "unique.txt")
    _git(
        second,
        "-c",
        "commit.gpgsign=false",
        "-c",
        "user.name=Exact Author",
        "-c",
        "user.email=exact@example.test",
        "commit",
        "-q",
        "-m",
        "unique",
    )
    second_id = _git(second, "rev-parse", "HEAD")
    inspector = GitPortfolioInspector(context_id="context-1")
    occurrences = inspector.discover(tmp_path)
    normalized = normalize_repositories(
        occurrences,
        [inspector.observe(item) for item in occurrences],
    )[0]
    logical = LogicalRepository(
        normalized.logical_repository_id,
        tuple(item.occurrence_id for item in occurrences),
        normalized.context_id,
        normalized.period_ids,
        normalized.author_match,
        normalized.first_commit_id,
        normalized.last_commit_id,
        normalized.qualifying_commit_count,
        normalized.research_status,
        normalized.deep_research_decision,
        normalized.deep_research_reason,
        normalized.conflict_note,
        normalized.observed_refs,
        normalized.commit_set_digest,
    )

    commits = inspector.commits(logical)

    expected = {
        first_id,
        second_id,
        *_git(second, "rev-list", "HEAD").splitlines(),
    }
    assert {item.commit_id for item in commits} == expected


def test_commits_includes_unique_detached_head_from_second_worktree(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    base_id = _repo(primary)
    worktree = tmp_path / "worktree"
    _git(primary, "worktree", "add", "-q", "--detach", str(worktree), "HEAD")
    (worktree / "detached.txt").write_text("detached\n", encoding="utf-8")
    _git(worktree, "add", "detached.txt")
    _git(
        worktree,
        "-c",
        "commit.gpgsign=false",
        "-c",
        "user.name=Exact Author",
        "-c",
        "user.email=exact@example.test",
        "commit",
        "-q",
        "-m",
        "detached",
    )
    detached_id = _git(worktree, "rev-parse", "HEAD")
    inspector = GitPortfolioInspector(context_id="context-1")
    occurrences = inspector.discover(tmp_path)
    logical = normalize_repositories(
        occurrences,
        [inspector.observe(item) for item in occurrences],
    )[0]

    commits = inspector.commits(logical)

    assert {item.commit_id for item in commits} == {base_id, detached_id}


@pytest.mark.parametrize("code", ["timeout", "overflow", "malformed", "git-error"])
def test_discovery_propagates_transient_git_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    code: str,
) -> None:
    _repo(tmp_path / "repo")
    inspector = GitPortfolioInspector(context_id="context-1")
    monkeypatch.setattr(
        inspector,
        "_observe_path",
        lambda *_args: (_ for _ in ()).throw(GitPortfolioIssue(code, "blocked")),
    )

    with pytest.raises(GitPortfolioIssue) as caught:
        inspector.discover(tmp_path)
    assert caught.value.code == code


@pytest.mark.parametrize(
    "remote",
    [
        "https://user:secret@EXAMPLE.com/Owner/Repo.git",
        "ssh://user@Example.com/Owner/Repo.git",
        "user@EXAMPLE.com:Owner/Repo.git",
    ],
)
def test_remote_normalization_removes_credentials_and_normalizes_host(remote: str) -> None:
    assert _normalize_remote(remote) == "example.com/Owner/Repo"


def test_streaming_output_bound_terminates_before_buffering_unbounded_output() -> None:
    inspector = GitPortfolioInspector(context_id="context-1", max_output_bytes=128)

    with pytest.raises(GitPortfolioIssue, match="configured bound") as caught:
        inspector._run_command(
            [sys.executable, "-c", "import sys; sys.stdout.write('x' * 1000000)"],
        )

    assert caught.value.code == "overflow"


def _occurrence(occurrence_id: str, path: str, common: str) -> RawRepositoryOccurrence:
    return RawRepositoryOccurrence(
        occurrence_id,
        Path(path),
        "standard",
        Path(common),
        "objects",
        ("refs/heads/main=" + "a" * 40,),
        "provisional-" + occurrence_id,
        None,
        "resolved",
    )


def _observation(
    occurrence_id: str,
    common: str,
    remote: str | None,
    commits: tuple[str, ...],
) -> RepositoryObservation:
    return RepositoryObservation(
        occurrence_id=occurrence_id,
        context_id="context-1",
        git_common_dir=Path(common),
        object_store_id="objects",
        remote_identity=remote,
        observed_refs=("refs/heads/main=" + commits[-1],),
        reachable_commits=commits,
    )
