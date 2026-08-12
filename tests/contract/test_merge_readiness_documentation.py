from __future__ import annotations

import ast
from collections.abc import Callable
from contextlib import AbstractContextManager
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, cast

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SOLUTION = (
    REPO_ROOT
    / "docs/solutions/workflow-issues/"
    "merge-readiness-requires-review-freshness-and-git-backed-verification.md"
)
HARDENING = REPO_ROOT / "docs/deviations/2026-08-03-pr42-review-hardening-003.md"


def _fenced_blocks(path: Path, language: str) -> list[str]:
    text = path.read_text(encoding="utf-8")
    return re.findall(rf"```{language}\n(.*?)```", text, flags=re.DOTALL)


def _gate_sources() -> tuple[str, str]:
    bash_blocks = _fenced_blocks(SOLUTION, "bash")
    assert len(bash_blocks) >= 2, "the solution must contain audit and verification blocks"
    gate = bash_blocks[0]
    match = re.search(r"<<'PY'\n(.*?)\nPY", gate, flags=re.DOTALL)
    assert match, "the review audit must contain embedded Python"
    return gate, match.group(1)


def _function_namespace(source: str) -> dict[str, Any]:
    tree = ast.parse(source)
    nodes: list[ast.stmt] = [
        node
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef))
    ]
    namespace: dict[str, Any] = {}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<fixture>", "exec"), namespace)
    return namespace


def _check_contracts() -> list[dict[str, object]]:
    return [
        {"label": "first", "app_id": 11, "check_name": "First Review"},
        {"label": "second", "app_id": 22, "check_name": "Second Review"},
    ]


def _check_run(
    *,
    check_id: int,
    app_id: int,
    name: str,
    head: str,
) -> dict[str, object]:
    return {
        "id": check_id,
        "name": name,
        "app": {"id": app_id},
        "head_sha": head,
        "status": "completed",
        "conclusion": "success",
        "pull_requests": [
            {
                "number": 42,
                "base": {"repo": {"id": 101, "name": "repo"}},
                "head": {"repo": {"id": 202, "name": "fork"}},
            }
        ],
    }


def _fake_gh(
    check_runs: list[dict[str, object]],
    *,
    thread_head: str | None = None,
    threads: list[dict[str, object]] | None = None,
) -> Callable[..., object]:
    head = "a" * 40
    graphql_head = thread_head or head
    review_threads = threads
    if review_threads is None:
        review_threads = [
            {
                "isResolved": True,
                "resolvedBy": {"login": "maintainer"},
            }
        ]

    def fake(*arguments: str) -> object:
        joined = " ".join(arguments)
        if arguments[:2] == ("repo", "view"):
            return {"nameWithOwner": "owner/repo"}
        if arguments[:2] == ("pr", "view"):
            return {
                "baseRefName": "main",
                "headRefOid": head,
                "headRepository": {"nameWithOwner": "owner/fork"},
            }
        if arguments[:2] == ("api", "repos/owner/repo"):
            return {"id": 101}
        if arguments[:2] == ("api", "repos/owner/fork"):
            return {"id": 202}
        if "check-runs" in joined:
            return [{"check_runs": check_runs}]
        if "/comments" in joined:
            return [[]]
        if "graphql" in arguments:
            return [
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "headRefOid": graphql_head,
                                "reviewThreads": {
                                    "nodes": review_threads
                                },
                            }
                        }
                    }
                }
            ]
        raise AssertionError(f"unexpected gh call: {arguments}")

    return fake


def _run_audit_fixture(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    gate, _ = _gate_sources()
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    calls = tmp_path / "calls.jsonl"
    gh_stub = stub_bin / "gh"
    gh_stub.write_text(
        """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

arguments = sys.argv[1:]
calls = Path(os.environ["STUB_GH_CALLS"])
with calls.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(arguments) + "\\n")

joined = " ".join(arguments)
head = "a" * 40
if arguments[:2] == ["repo", "view"]:
    payload = {"nameWithOwner": "owner/repo"}
elif arguments[:2] == ["pr", "view"]:
    payload = {
        "baseRefName": "main",
        "headRefOid": head,
        "headRepository": {"nameWithOwner": "owner/fork"},
    }
elif arguments[:2] == ["api", "repos/owner/repo"]:
    payload = {"id": 101}
elif arguments[:2] == ["api", "repos/owner/fork"]:
    payload = {"id": 202}
elif "check-runs" in joined and "--method" not in arguments:
    payload = [{
        "check_runs": [{
            "id": 1,
            "name": "Automated Review",
            "app": {"id": 123456},
            "head_sha": head,
            "status": "completed",
            "conclusion": "success",
            "pull_requests": [{
                "number": 42,
                "base": {"repo": {"id": 101, "name": "repo"}},
                "head": {"repo": {"id": 202, "name": "fork"}},
            }],
        }]
    }]
elif "/comments" in joined and "--method" not in arguments:
    payload = [[]]
elif "graphql" in arguments:
    query_fields = [item for item in arguments if item.startswith("query=")]
    if len(query_fields) != 1:
        raise SystemExit(98)
    query = query_fields[0].removeprefix("query=").lstrip()
    if not query.startswith("query(") or "mutation" in query:
        raise SystemExit(98)
    payload = [{
        "data": {
            "repository": {
                "pullRequest": {
                    "headRefOid": head,
                    "reviewThreads": {
                        "nodes": [{
                            "isResolved": True,
                            "resolvedBy": {"login": "example-maintainer"},
                        }]
                    },
                }
            }
        }
    }]
else:
    raise SystemExit(97)
print(json.dumps(payload))
""",
        encoding="utf-8",
    )
    gh_stub.chmod(0o755)
    script = tmp_path / "audit.sh"
    _write_executable(script, gate)
    return subprocess.run(
        ["bash", str(script), "42"],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{stub_bin}:{os.environ['PATH']}",
            "STUB_GH_CALLS": str(calls),
        },
        text=True,
        capture_output=True,
        check=False,
    )


def test_check_configuration_is_unique_and_drives_complete_audit() -> None:
    gate, source = _gate_sources()
    namespace = _function_namespace(source)
    check_contracts = namespace["check_contracts"]
    contracts = _check_contracts()

    assert check_contracts(contracts) == contracts
    with pytest.raises(SystemExit, match="duplicate required check identity"):
        check_contracts([contracts[0], {**contracts[0], "label": "duplicate"}])

    head = "a" * 40
    runs = [
        _check_run(check_id=1, app_id=11, name="First Review", head=head),
        _check_run(check_id=2, app_id=22, name="Second Review", head=head),
    ]
    snapshot = namespace["audit_pull_request"](
        "42",
        contracts,
        {"maintainer"},
        _fake_gh(runs),
    )

    assert snapshot["status"] == "audit-passed"
    assert snapshot["captured_head"] == head
    assert "audit_pull_request(" in gate
    with pytest.raises(SystemExit, match="required check is absent for second"):
        namespace["audit_pull_request"](
            "42",
            contracts,
            {"maintainer"},
            _fake_gh(runs[:1]),
        )


def test_complete_audit_script_executes_only_allowlisted_reads(tmp_path: Path) -> None:
    result = _run_audit_fixture(tmp_path)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "audit-passed"
    calls = [
        json.loads(line)
        for line in (tmp_path / "calls.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert calls[:4] == [
        ["repo", "view", "--json", "nameWithOwner"],
        [
            "pr",
            "view",
            "42",
            "--json",
            "baseRefName,headRefOid,headRepository",
        ],
        ["api", "repos/owner/repo"],
        ["api", "repos/owner/fork"],
    ]
    assert len(calls) == 7
    assert calls[4] == [
        "api",
        "--paginate",
        "--slurp",
        f"repos/owner/repo/commits/{'a' * 40}/check-runs?per_page=100",
    ]
    assert calls[5] == [
        "api",
        "--paginate",
        "--slurp",
        "repos/owner/repo/pulls/42/comments?per_page=100",
    ]
    graphql_call = calls[6]
    assert graphql_call[:4] == ["api", "graphql", "--paginate", "--slurp"]
    query_fields = [item for item in graphql_call if item.startswith("query=")]
    assert len(query_fields) == 1
    assert query_fields[0].removeprefix("query=").lstrip().startswith("query(")
    assert "mutation" not in query_fields[0]
    assert all("--method" not in call and "-X" not in call for call in calls)


def test_audit_has_no_client_merge_or_branch_scoped_inventory() -> None:
    gate, source = _gate_sources()
    assert "gh pr merge" not in gate
    assert "actions/workflows" not in gate
    assert "head_branch" not in gate
    assert "serialized_trigger_ledger" not in source
    assert "server-only" in gate


def test_check_run_requires_exact_target_pull_request_association() -> None:
    _, source = _gate_sources()
    namespace = _function_namespace(source)
    targets_pr = namespace["check_targets_pr"]
    target = _check_run(check_id=1, app_id=11, name="First Review", head="a" * 40)
    unrelated = {
        **target,
        "pull_requests": [
            {
                "number": 999,
                "base": {"repo": {"id": 101, "name": "repo"}},
                "head": {"repo": {"id": 202, "name": "fork"}},
            }
        ],
    }
    ambiguous = {
        **target,
        "pull_requests": [
            *cast(list[object], target["pull_requests"]),
            *cast(list[object], unrelated["pull_requests"]),
        ],
    }

    assert targets_pr(target, 42, 101, 202)
    assert not targets_pr(unrelated, 42, 101, 202)
    assert not targets_pr(ambiguous, 42, 101, 202)


def test_complete_audit_rejects_invalid_checks_associations_and_threads() -> None:
    _, source = _gate_sources()
    audit = _function_namespace(source)["audit_pull_request"]
    head = "a" * 40
    valid = _check_run(check_id=1, app_id=11, name="First Review", head=head)
    contract = [_check_contracts()[0]]
    cases = [
        ({**valid, "status": "in_progress"}, None, "required check is active"),
        ({**valid, "conclusion": "failure"}, None, "required check failed"),
        (
            {**valid, "pull_requests": [{"number": 999}]},
            None,
            "required check is absent",
        ),
        (
            valid,
            [{"isResolved": False, "resolvedBy": None}],
            "thread is unresolved",
        ),
        (
            valid,
            [{"isResolved": True, "resolvedBy": {"login": "contributor"}}],
            "thread is unresolved",
        ),
    ]

    for check_run, threads, message in cases:
        with pytest.raises(SystemExit, match=message):
            audit("42", contract, {"maintainer"}, _fake_gh([check_run], threads=threads))

    with pytest.raises(SystemExit, match="GraphQL head changed during pagination"):
        audit(
            "42",
            contract,
            {"maintainer"},
            _fake_gh([valid], thread_head="b" * 40),
        )


def test_resolved_threads_require_a_trusted_resolver() -> None:
    _, source = _gate_sources()
    namespace = _function_namespace(source)
    trusted = namespace["thread_has_trusted_resolution"]

    author_resolution = {"isResolved": True, "resolvedBy": {"login": "contributor"}}
    maintainer_resolution = {"isResolved": True, "resolvedBy": {"login": "maintainer"}}

    assert not trusted(author_resolution, {"maintainer"})
    assert trusted(maintainer_resolution, {"maintainer"})


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def _run_verification_fixture(
    tmp_path: Path,
    *,
    uv_exit: int = 0,
    remove_exit: int = 0,
    list_fail_at: int = 0,
) -> subprocess.CompletedProcess[str]:
    verification = _fenced_blocks(SOLUTION, "bash")[1]
    tmp_path.mkdir()
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    registration = tmp_path / "registration"
    list_count = tmp_path / "list-count"
    _write_executable(
        stub_bin / "gh",
        """#!/bin/sh
case "$*" in
  *mergeCommit*) printf '%s\n' merge-oid ;;
  *baseRefName*) printf '%s\n' main ;;
  *) exit 2 ;;
esac
""",
    )
    _write_executable(
        stub_bin / "git",
        """#!/bin/sh
case "$1 $2" in
  "fetch origin") exit 0 ;;
  "worktree add") mkdir -p "$4"; printf '%s\n' "$4" > "$STUB_REGISTRATION" ;;
  "worktree list")
    count=0
    [ -f "$STUB_LIST_COUNT" ] && count="$(cat "$STUB_LIST_COUNT")"
    count=$((count + 1))
    printf '%s\n' "$count" > "$STUB_LIST_COUNT"
    [ "$STUB_LIST_FAIL_AT" -eq "$count" ] && exit 2
    if [ -f "$STUB_REGISTRATION" ]; then
      printf 'worktree %s\n' "$(cat "$STUB_REGISTRATION")"
    fi
    ;;
  "worktree remove")
    [ "$STUB_REMOVE_EXIT" -ne 0 ] && exit "$STUB_REMOVE_EXIT"
    path="$(cat "$STUB_REGISTRATION")"
    rm -rf "$path" "$STUB_REGISTRATION"
    ;;
  *) exit 2 ;;
esac
""",
    )
    _write_executable(
        stub_bin / "uv",
        """#!/bin/sh
[ "$1" = build ] && exit "$STUB_UV_EXIT"
exit 0
""",
    )
    _write_executable(stub_bin / "npm", "#!/bin/sh\nexit 0\n")
    script = tmp_path / "verify.sh"
    _write_executable(script, verification)
    env = {
        **os.environ,
        "PATH": f"{stub_bin}:{os.environ['PATH']}",
        "STUB_LIST_COUNT": str(list_count),
        "STUB_LIST_FAIL_AT": str(list_fail_at),
        "STUB_REGISTRATION": str(registration),
        "STUB_REMOVE_EXIT": str(remove_exit),
        "STUB_UV_EXIT": str(uv_exit),
    }
    return subprocess.run(
        ["bash", str(script), "42"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_worktree_cleanup_is_fail_closed_and_preserves_status(tmp_path: Path) -> None:
    verification = _fenced_blocks(SOLUTION, "bash")[1]
    assert verification.index("trap cleanup EXIT") < verification.index("git worktree add")
    assert "worktree cleanup failed" in verification

    remove_failure = _run_verification_fixture(tmp_path / "remove", remove_exit=9)
    assert remove_failure.returncode != 0
    assert "worktree cleanup failed" in remove_failure.stderr

    build_failure = _run_verification_fixture(tmp_path / "build", uv_exit=23)
    assert build_failure.returncode == 23

    list_failure = _run_verification_fixture(tmp_path / "list", list_fail_at=2)
    assert list_failure.returncode != 0
    assert "worktree cleanup failed" in list_failure.stderr

    success = _run_verification_fixture(tmp_path / "success")
    assert success.returncode == 0


def _hardening_namespace() -> dict[str, Any]:
    if not HARDENING.exists():
        pytest.skip("the curated export excludes the lifecycle hardening addendum")
    blocks = _fenced_blocks(HARDENING, "python")
    assert blocks, "the hardening addendum must contain executable fixture helpers"
    namespace: dict[str, Any] = {}
    exec(compile(blocks[0], str(HARDENING), "exec"), namespace)
    return namespace


def _lock_call(
    namespace: dict[str, Any],
    repository_root: Path,
    *,
    repository: str = "owner/repo",
) -> AbstractContextManager[None]:
    ship_lock = cast(Callable[..., AbstractContextManager[None]], namespace["ship_lock"])
    return ship_lock(
        repository_root,
        999,
        repository,
        42,
        transition="T3",
        acquired_at="2026-08-03T00:00:00Z",
        process_id="host:123",
    )


def _linked_worktrees(tmp_path: Path) -> tuple[Path, Path, Path]:
    primary = tmp_path / "primary"
    linked = tmp_path / "linked"
    subprocess.run(["git", "init", str(primary)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(primary), "config", "user.email", "fixture@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(primary), "config", "user.name", "Fixture"],
        check=True,
    )
    (primary / "fixture.txt").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(primary), "add", "fixture.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(primary), "commit", "-m", "fixture"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(primary), "worktree", "add", "-b", "linked", str(linked)],
        check=True,
        capture_output=True,
    )
    common_dir = primary / ".git"
    return primary, linked, common_dir


def test_ship_lock_and_generation_cas_reject_concurrency(tmp_path: Path) -> None:
    namespace = _hardening_namespace()
    primary, linked, common_dir = _linked_worktrees(tmp_path)
    for invalid_repository_id in ("999", True, 0, -1, "../../../outside"):
        with pytest.raises(ValueError, match="repository ID must be a positive integer"):
            namespace["canonical_ship_lock_path"](
                primary,
                invalid_repository_id,
                42,
            )
    for invalid_pull_request in ("42", True, 0, -1, "../../../outside"):
        with pytest.raises(ValueError, match="pull request must be a positive integer"):
            namespace["canonical_ship_lock_path"](
                primary,
                999,
                invalid_pull_request,
            )
    primary_lock = namespace["canonical_ship_lock_path"](primary, 999, 42)
    renamed_lock = namespace["canonical_ship_lock_path"](linked, 999, 42)
    assert primary_lock == renamed_lock
    ledger_path = tmp_path / "progress.json"
    ledger_path.write_text('{"generation":1}\n', encoding="utf-8")
    expected_hash = namespace["content_hash"](ledger_path.read_bytes())

    with _lock_call(namespace, primary):
        owner_files = list((common_dir / "release-loop-locks").rglob("owner.json"))
        assert len(owner_files) == 1
        owner = json.loads(owner_files[0].read_text(encoding="utf-8"))
        assert owner["repository"] == "owner/repo"
        assert owner["repository_id"] == 999
        assert owner["pull_request"] == 42
        assert owner["acquired_at"] == "2026-08-03T00:00:00Z"
        with pytest.raises(RuntimeError, match="ship lock already exists"):
            with _lock_call(namespace, linked, repository="new-owner/new-name"):
                pass
        namespace["replace_ledger"](
            ledger_path,
            1,
            expected_hash,
            b'{"generation":2}\n',
        )

    with pytest.raises(RuntimeError, match="ledger generation must advance exactly once"):
        namespace["replace_ledger"](
            ledger_path,
            2,
            namespace["content_hash"](ledger_path.read_bytes()),
            b'{"generation":4}\n',
        )

    current_payload = ledger_path.read_bytes()
    with pytest.raises(RuntimeError, match="ledger generation changed concurrently"):
        namespace["replace_ledger"](
            ledger_path,
            1,
            namespace["content_hash"](current_payload),
            b'{"generation":3}\n',
        )
    assert ledger_path.read_bytes() == current_payload

    ledger_path.write_text('{"generation":1}\n', encoding="utf-8")
    expected_hash = namespace["content_hash"](ledger_path.read_bytes())

    def competing_writer() -> None:
        ledger_path.write_text('{"generation":2,"writer":"other"}\n', encoding="utf-8")

    with pytest.raises(RuntimeError, match="ledger changed concurrently during replacement"):
        namespace["replace_ledger"](
            ledger_path,
            1,
            expected_hash,
            b'{"generation":2,"writer":"ours"}\n',
            before_replace=competing_writer,
        )
    assert json.loads(ledger_path.read_text(encoding="utf-8"))["writer"] == "other"
    assert not list(tmp_path.glob(".progress.json.*.tmp"))

    ledger_path.write_text('{"generation":2,"writer":"new"}\n', encoding="utf-8")
    with pytest.raises(RuntimeError, match="ledger changed concurrently before replacement"):
        namespace["replace_ledger"](
            ledger_path,
            2,
            expected_hash,
            b'{"generation":3}\n',
        )
    assert json.loads(ledger_path.read_text(encoding="utf-8"))["writer"] == "new"
    assert not list(tmp_path.glob(".progress.json.*.tmp"))


def test_t3_intent_is_durable_and_recovery_is_exact(tmp_path: Path) -> None:
    namespace = _hardening_namespace()
    intent_path = tmp_path / "t3-intent.json"
    intent = {
        "transition_id": "T3-1",
        "created_at": "2026-08-03T00:00:00Z",
        "baseline_comment_ids": [1],
        "actor_id": 100,
        "actor_login": "release-bot",
        "captured_head": "a" * 40,
        "repository": "owner/repo",
        "repository_id": 999,
        "pull_request": 42,
    }
    post_calls: list[str] = []

    def post_comment(body: str) -> dict[str, object]:
        assert intent_path.exists()
        post_calls.append(body)
        return {"id": 2}

    namespace["begin_t3_comment"](intent_path, intent, post_comment)
    assert post_calls == ["@codex review"]
    with pytest.raises(RuntimeError, match="T3 intent already exists"):
        namespace["begin_t3_comment"](intent_path, intent, post_comment)
    assert post_calls == ["@codex review"]
    with pytest.raises(RuntimeError, match="T3 intent lacks fields"):
        namespace["write_t3_intent"](tmp_path / "incomplete.json", {"transition_id": "T3-2"})
    recovered_intent = json.loads(intent_path.read_text(encoding="utf-8"))
    candidate = {
        "id": 2,
        "body": "@codex review",
        "created_at": "2026-08-03T00:00:00Z",
        "user": {"id": 100, "login": "release-bot"},
    }

    recovered = namespace["reconcile_t3_comment"](
        recovered_intent,
        [candidate],
        current_head="a" * 40,
        repository_id=999,
        pull_request=42,
    )
    assert recovered["id"] == 2

    for invalid in (
        {**candidate, "body": " @codex review"},
        {**candidate, "body": "@codex review\n"},
        {**candidate, "user": {"id": 999, "login": "attacker"}},
    ):
        with pytest.raises(RuntimeError, match="expected one recoverable T3 comment"):
            namespace["reconcile_t3_comment"](
                recovered_intent,
                [invalid],
                current_head="a" * 40,
                repository_id=999,
                pull_request=42,
            )

    with pytest.raises(RuntimeError, match="T3 intent head changed"):
        namespace["reconcile_t3_comment"](
            recovered_intent,
            [candidate],
            current_head="b" * 40,
            repository_id=999,
            pull_request=42,
        )
    with pytest.raises(RuntimeError, match="expected one recoverable T3 comment"):
        namespace["reconcile_t3_comment"](
            recovered_intent,
            [candidate, {**candidate, "id": 3}],
            current_head="a" * 40,
            repository_id=999,
            pull_request=42,
        )

    with pytest.raises(RuntimeError, match="T3 intent target changed"):
        namespace["reconcile_t3_comment"](
            recovered_intent,
            [candidate],
            current_head="a" * 40,
            repository_id=1000,
            pull_request=42,
        )
    with pytest.raises(RuntimeError, match="T3 intent target changed"):
        namespace["reconcile_t3_comment"](
            recovered_intent,
            [candidate],
            current_head="a" * 40,
            repository_id=999,
            pull_request=43,
        )
    with pytest.raises(RuntimeError, match="expected one recoverable T3 comment"):
        namespace["reconcile_t3_comment"](
            recovered_intent,
            [{**candidate, "created_at": "2026-08-02T23:59:59Z"}],
            current_head="a" * 40,
            repository_id=999,
            pull_request=42,
        )
