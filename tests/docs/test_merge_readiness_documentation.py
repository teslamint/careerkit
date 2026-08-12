from __future__ import annotations

import ast
from collections.abc import Callable
from contextlib import AbstractContextManager
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
HARDENING = REPO_ROOT / "docs/deviations/2026-08-03-pr42-review-hardening-002.md"


def _fenced_blocks(path: Path, language: str) -> list[str]:
    text = path.read_text(encoding="utf-8")
    return re.findall(rf"```{language}\n(.*?)```", text, flags=re.DOTALL)


def _gate_sources() -> tuple[str, str]:
    bash_blocks = _fenced_blocks(SOLUTION, "bash")
    assert len(bash_blocks) >= 2, "the solution must contain gate and verification blocks"
    gate = bash_blocks[0]
    assert "audit_pull_request" in gate, "block 0 is not the review gate"
    match = re.search(r"<<'PY'\n(.*?)\nPY", gate, flags=re.DOTALL)
    assert match, "the review gate must contain embedded Python"
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


def test_check_contracts_validates_required_checks() -> None:
    _, source = _gate_sources()
    namespace = _function_namespace(source)
    assert "check_contracts" in namespace

    check_contracts = namespace["check_contracts"]
    contracts = check_contracts([
        {"label": "first", "app_id": 1, "check_name": "First Review"},
        {"label": "second", "app_id": 2, "check_name": "Second Review"},
    ])
    assert len(contracts) == 2

    with pytest.raises(SystemExit, match="required check configuration is empty"):
        check_contracts([])

    with pytest.raises(SystemExit, match="a required check lacks a label"):
        check_contracts([{"app_id": 1, "check_name": "X"}])

    with pytest.raises(SystemExit, match="duplicate required check identity"):
        check_contracts([
            {"label": "a", "app_id": 1, "check_name": "X"},
            {"label": "b", "app_id": 1, "check_name": "X"},
        ])


def test_audit_output_is_not_merge_authority() -> None:
    gate, source = _gate_sources()
    assert "gh pr merge" not in gate
    assert '"merge_authority": "server-only"' in source or "'merge_authority': 'server-only'" in source


def test_merge_gate_does_not_use_branch_scoped_workflow_inventory() -> None:
    gate, _ = _gate_sources()
    assert "actions/workflows" not in gate
    assert "head_branch" not in gate
    assert "gh pr merge" not in gate
    assert "server-only" in gate


def test_stale_head_check_run_is_excluded_from_matching_checks() -> None:
    gate, source = _gate_sources()
    assert "head_sha" in gate, "the gate must filter check-runs by head_sha"
    assert 'run.get("head_sha") == captured_head' in source


def test_resolved_threads_require_a_trusted_resolver() -> None:
    _, source = _gate_sources()
    namespace = _function_namespace(source)
    assert "thread_has_trusted_resolution" in namespace
    trusted = namespace["thread_has_trusted_resolution"]

    author_resolution = {"isResolved": True, "resolvedBy": {"login": "contributor"}}
    maintainer_resolution = {"isResolved": True, "resolvedBy": {"login": "maintainer"}}

    assert not trusted(author_resolution, {"maintainer"})
    assert trusted(maintainer_resolution, {"maintainer"})


def test_check_targets_pr_validates_association() -> None:
    _, source = _gate_sources()
    namespace = _function_namespace(source)
    assert "check_targets_pr" in namespace
    check_targets_pr = namespace["check_targets_pr"]

    valid_run = {
        "pull_requests": [{
            "number": 42,
            "base": {"repo": {"id": 101}},
            "head": {"repo": {"id": 101}},
        }],
    }
    assert check_targets_pr(valid_run, 42, 101, 101)
    assert not check_targets_pr(valid_run, 99, 101, 101)
    assert not check_targets_pr({"pull_requests": []}, 42, 101, 101)
    assert not check_targets_pr({"pull_requests": None}, 42, 101, 101)


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def test_worktree_cleanup_is_armed_before_mutation_and_cannot_hide_failure(
    tmp_path: Path,
) -> None:
    verification = _fenced_blocks(SOLUTION, "bash")[1]
    assert "verify-exact-merge" in verification, "block 1 is not the verification block"
    assert verification.index("trap cleanup EXIT") < verification.index("git worktree add")
    assert "git worktree remove --force \"$verify_root\" >/dev/null 2>&1 || true" not in verification
    assert "worktree cleanup failed" in verification
    assert "worktree $verify_root" in verification

    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    registration = tmp_path / "registration"
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
  "worktree add") mkdir -p "$4"; resolved="$(cd "$4" && pwd -P)"; printf '%s\n' "$resolved" > "$STUB_REGISTRATION" ;;
  "worktree list")
    if [ -f "$STUB_REGISTRATION" ]; then
      printf 'worktree %s\n' "$(cat "$STUB_REGISTRATION")"
    fi
    ;;
  "worktree remove") exit 1 ;;
  *) exit 2 ;;
esac
""",
    )
    for command in ("uv", "npm"):
        _write_executable(stub_bin / command, "#!/bin/sh\nexit 0\n")
    script = tmp_path / "verify.sh"
    _write_executable(script, verification)
    env = {
        **os.environ,
        "PATH": f"{stub_bin}:{os.environ['PATH']}",
        "STUB_REGISTRATION": str(registration),
    }

    result = subprocess.run(
        ["bash", str(script), "42"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "worktree cleanup failed" in result.stderr


def _hardening_namespace() -> dict[str, Any]:
    if not HARDENING.exists():
        pytest.skip("the curated export excludes the lifecycle hardening addendum")
    blocks = _fenced_blocks(HARDENING, "python")
    assert blocks, "the hardening addendum must contain executable fixture helpers"
    assert "ship_lock" in blocks[0], "block 0 is not the ship-lock helpers"
    namespace: dict[str, Any] = {}
    exec(compile(blocks[0], str(HARDENING), "exec"), namespace)
    return namespace


def test_ship_lock_and_ledger_compare_and_swap_reject_concurrency(tmp_path: Path) -> None:
    namespace = _hardening_namespace()
    ship_lock = cast(
        Callable[[Path, dict[str, str]], AbstractContextManager[None]],
        namespace["ship_lock"],
    )
    replace_ledger = namespace["replace_ledger"]
    lock_path = tmp_path / "ship.lock"
    ledger_path = tmp_path / "progress.json"
    ledger_path.write_text('{"generation":1}\n', encoding="utf-8")
    expected_hash = namespace["content_hash"](ledger_path.read_bytes())

    with ship_lock(lock_path, {"transition": "T3"}):
        with pytest.raises(RuntimeError, match="ship lock already exists"):
            with ship_lock(lock_path, {"transition": "T3"}):
                pass

    replace_ledger(ledger_path, expected_hash, b'{"generation":2}\n')
    with pytest.raises(RuntimeError, match="ledger changed concurrently"):
        replace_ledger(ledger_path, expected_hash, b'{"generation":3}\n')


def test_t3_intent_recovery_accepts_exactly_one_new_comment() -> None:
    namespace = _hardening_namespace()
    reconcile = namespace["reconcile_t3_comment"]
    intent = {
        "created_at": "2026-08-03T00:00:00Z",
        "baseline_comment_ids": [1],
    }
    candidate = {
        "id": 2,
        "body": "@codex review",
        "created_at": "2026-08-03T00:00:01Z",
    }

    assert reconcile(intent, [candidate])["id"] == 2
    with pytest.raises(RuntimeError, match="expected one recoverable T3 comment"):
        reconcile(intent, [])
    with pytest.raises(RuntimeError, match="expected one recoverable T3 comment"):
        reconcile(intent, [candidate, {**candidate, "id": 3}])
