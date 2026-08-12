---
module: release-loop
date: "2026-08-02"
last_updated: "2026-08-03"
problem_type: workflow_issue
component: merge-verification
severity: high
applies_when:
  - "PR merge readiness depends on automated review results"
  - "zero unresolved threads is used as a merge gate"
  - "repository tests use Git-aware commands"
  - "the exact merge commit needs local verification"
tags:
  - merge-readiness
  - review-freshness
  - review-threads
  - detached-worktree
  - git-metadata
  - verification
---

# Merge Readiness Requires Review Freshness and Git-Backed Verification

## Context

A zero unresolved-thread count proves only the observed thread state.
It does not prove that the latest review inspected the current PR HEAD.

PR #38 reached zero unresolved threads after two review rounds.
A third automated review then inspected the current HEAD and added two threads.
The merge ran before another thread query.

The first merged-result check also used an archive export.
Several repository tests call `git ls-files` and `git checkout-index`.
Those tests cannot run correctly without Git metadata.

## Guidance

Bind review evidence and verification evidence to exact commit identifiers.

1. Declare each required reviewer login.
2. Audit one commit-bound check for each reviewer.
3. Treat a serialized trigger ledger as audit evidence only.
4. Capture the pull request head before the final review checks.
5. Read REST inline review comments.
6. Read GraphQL review threads and resolver identities.
7. Require a trusted resolver for every resolved thread.
8. Reject every incomplete or ambiguous review lifecycle.
9. Give the snapshot to a protected server-side policy.
10. Do not merge from the client audit.
11. Read the exact merge commit object identifier after the merge.
12. Create a detached Git worktree at that object identifier.
13. Build and test inside the worktree with locked dependencies.

Use both review APIs because they answer different questions.
REST exposes inline comments. GraphQL exposes thread resolution state.

A configured check must exist on the captured head.
Its status must be `completed`. Its conclusion must be `success`.
The audit supports same-repository pull requests only.
GitHub's check-runs API returns an empty `pull_requests` array for fork PRs.

A ledger reviewer can record three trigger types:

- Pull request readiness
- Every head-changing push that starts another review
- An explicit final review request

Serialize these triggers for audit evidence.
Do not use this evidence as a server-side merge precondition.

Each trigger needs one or more later completion signals.
Map every completion signal to exactly one trigger.
A review submission must name the trigger head commit.
A success reaction must belong to the exact trigger comment.
The head must not change between that trigger and reaction.

Reject missing, queued, active, cancelled, unsuccessful, or ambiguous evidence.
A completed review list does not prove that every configured trigger completed.
A client ledger cannot prevent a new same-head review event after its final snapshot.

## Why This Matters

A review can become stale after any push.
An unresolved-thread query can also become stale before the merge command runs.
The client audit cannot close a same-head review race.
A protected server policy must recompute the state before merge.

An archive contains tracked file content but no repository metadata.
It can produce false failures for Git-aware tests.
A detached worktree contains the exact tree and the required metadata.

Fail-fast shell settings prevent a missing commit from becoming partial evidence.
Use `set -euo pipefail` before export, build, or test commands.

## When to Apply

- Before merging a PR that receives asynchronous automated reviews
- After any push that follows review feedback
- When a merge gate reports zero unresolved threads
- When repository tests execute Git commands
- When the merged result differs from the feature branch history

## Examples

### Check review completeness and freshness

Keep a JSON ledger for reviewers that do not publish a required check.
Use this ledger for audit evidence only.
Record a trigger before its asynchronous review starts.
Record each completion when its live signal arrives.

```json
{
  "triggers": [
    {
      "sequence": 1,
      "reviewer": "chatgpt-codex-connector[bot]",
      "type": "ready",
      "event_id": "pr-created:42",
      "head": "0123456789abcdef0123456789abcdef01234567",
      "created_at": "2026-08-03T00:00:00Z",
      "completions": [
        {
          "type": "review",
          "id": 1001,
          "created_at": "2026-08-03T00:01:00Z"
        }
      ]
    },
    {
      "sequence": 2,
      "reviewer": "chatgpt-codex-connector[bot]",
      "type": "explicit",
      "event_id": "comment:2001",
      "comment_id": 2001,
      "head": "0123456789abcdef0123456789abcdef01234567",
      "created_at": "2026-08-03T00:02:00Z",
      "completions": [
        {
          "type": "reaction",
          "id": 3001,
          "comment_id": 2001,
          "created_at": "2026-08-03T00:03:00Z"
        }
      ]
    }
  ]
}
```

Use `synchronize` for every head-changing push trigger.
Use `push:<sequence>:<head>` as its append-only `event_id`.
Use the exact `@codex review` comment identifier for an explicit trigger.

The following audit needs GitHub CLI and Python 3.
Replace the example check labels, immutable application identifiers, names, and trusted resolvers.
The audit never merges or queues the pull request.
It reports one commit-bound snapshot for an operator or protected server-side check.
A merge requires server rules that enforce both review completion and thread policy.
If the repository lacks those rules, this workflow does not authorize a merge.

```bash
#!/usr/bin/env bash
set -euo pipefail

pr="${1:?usage: audit-required-review-state <pull-request-number>}"

python3 - "$pr" <<'PY'
import json
import subprocess
import sys

pr = sys.argv[1]

TRUSTED_THREAD_RESOLVERS = {"example-maintainer"}
REQUIRED_CHECKS = [
    {
        "label": "automated-review",
        "app_id": 123456,
        "check_name": "Automated Review",
    },
]


def require(condition, message):
    if not condition:
        raise SystemExit(f"review audit failed: {message}")


def check_contracts(contracts):
    require(isinstance(contracts, list) and contracts, "required check configuration is empty")
    identities = []
    for contract in contracts:
        require(isinstance(contract, dict), "a required check contract is not an object")
        require(contract.get("label"), "a required check lacks a label")
        require(isinstance(contract.get("app_id"), int), "a required check lacks an app ID")
        require(contract.get("check_name"), "a required check lacks a name")
        identity = (contract["app_id"], contract["check_name"])
        require(identity not in identities, "duplicate required check identity")
        identities.append(identity)
    return contracts


def check_targets_pr(run, pr_number, base_repository_id, head_repository_id):
    associations = run.get("pull_requests") or []
    if len(associations) != 1:
        return False
    association = associations[0]
    return (
        association.get("number") == pr_number
        and association.get("base", {}).get("repo", {}).get("id") == base_repository_id
        and association.get("head", {}).get("repo", {}).get("id") == head_repository_id
    )


def thread_has_trusted_resolution(thread, trusted_resolvers):
    resolver = thread.get("resolvedBy") or {}
    return thread.get("isResolved") is True and resolver.get("login") in trusted_resolvers


def gh_json(*arguments):
    result = subprocess.run(
        ["gh", *arguments],
        check=True,
        text=True,
        capture_output=True,
    )
    return json.loads(result.stdout)


def audit_pull_request(pr, required_checks, trusted_resolvers, gh_json_fn=gh_json):
    contracts = check_contracts(required_checks)
    repository = gh_json_fn("repo", "view", "--json", "nameWithOwner")["nameWithOwner"]
    owner, name = repository.split("/", 1)
    pr_state = gh_json_fn(
        "pr",
        "view",
        pr,
        "--json",
        "baseRefName,headRefOid,headRepository",
    )
    pr_number = int(pr)
    captured_head = pr_state["headRefOid"]
    head_repository = pr_state["headRepository"]["nameWithOwner"]
    base_repository_id = gh_json_fn("api", f"repos/{repository}")["id"]
    head_repository_id = gh_json_fn("api", f"repos/{head_repository}")["id"]
    check_pages = gh_json_fn(
        "api",
        "--paginate",
        "--slurp",
        f"repos/{repository}/commits/{captured_head}/check-runs?per_page=100",
    )
    check_runs = [run for page in check_pages for run in page.get("check_runs", [])]
    comment_pages = gh_json_fn(
        "api",
        "--paginate",
        "--slurp",
        f"repos/{repository}/pulls/{pr}/comments?per_page=100",
    )
    inline_comments = [comment for page in comment_pages for comment in page]

    for contract in contracts:
        matching_checks = sorted(
            (
                run
                for run in check_runs
                if run.get("name") == contract["check_name"]
                and run.get("app", {}).get("id") == contract["app_id"]
                and run.get("head_sha") == captured_head
                and check_targets_pr(
                    run,
                    pr_number,
                    base_repository_id,
                    head_repository_id,
                )
            ),
            key=lambda run: run["id"],
        )
        require(matching_checks, f"required check is absent for {contract['label']}")
        latest_check = matching_checks[-1]
        require(latest_check.get("status") == "completed", "a required check is active")
        require(latest_check.get("conclusion") == "success", "a required check failed")

    thread_query = """
query($owner:String!,$name:String!,$number:Int!,$endCursor:String){
  repository(owner:$owner,name:$name){
    pullRequest(number:$number){
      headRefOid
      reviewThreads(first:100,after:$endCursor){
        nodes{isResolved resolvedBy{login}}
        pageInfo{hasNextPage endCursor}
      }
    }
  }
}
"""
    thread_pages = gh_json_fn(
        "api",
        "graphql",
        "--paginate",
        "--slurp",
        "-f",
        f"query={thread_query}",
        "-F",
        f"owner={owner}",
        "-F",
        f"name={name}",
        "-F",
        f"number={pr}",
    )
    threads = [
        thread
        for page in thread_pages
        for thread in page["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
    ]
    thread_heads = {
        page["data"]["repository"]["pullRequest"]["headRefOid"]
        for page in thread_pages
    }
    require(
        all(thread_has_trusted_resolution(thread, trusted_resolvers) for thread in threads),
        "a thread is unresolved or has an untrusted resolver",
    )
    require(thread_heads == {captured_head}, "the GraphQL head changed during pagination")
    return {
        "base_ref": pr_state["baseRefName"],
        "captured_head": captured_head,
        "head_repository": head_repository,
        "merge_authority": "server-only",
        "repository": repository,
        "required_checks": [contract["label"] for contract in contracts],
        "status": "audit-passed",
    }


snapshot = audit_pull_request(pr, REQUIRED_CHECKS, TRUSTED_THREAD_RESOLVERS)
print(json.dumps(snapshot, sort_keys=True))
PY
```

The audit reads REST inline comments and GraphQL review threads.
Every resolved thread must name a trusted resolver in the snapshot.
Every check run must identify the target pull request and both repositories.
The audit output is not merge authority.
A protected server-side policy must recompute review and thread state before merge.

### Verify the exact merge commit

```bash
#!/usr/bin/env bash
set -euo pipefail

pr="${1:?usage: verify-exact-merge <pull-request-number>}"
merge_oid="$(gh pr view "$pr" --json mergeCommit --jq '.mergeCommit.oid')"
base_ref="$(gh pr view "$pr" --json baseRefName --jq '.baseRefName')"
verify_root="$(mktemp -d)"
rmdir "$verify_root"
verify_root="$(python3 -c 'import os,sys;print(os.path.realpath(sys.argv[1]))' "$verify_root")"

cleanup() {
  exit_status=$?
  trap - EXIT
  registered=false
  if ! worktree_inventory="$(git worktree list --porcelain)"; then
    echo "worktree cleanup failed: registration inventory unavailable" >&2
    exit 1
  fi
  if grep -Fqx "worktree $verify_root" <<<"$worktree_inventory"; then
    registered=true
  fi
  if $registered && ! git worktree remove --force "$verify_root"; then
    echo "worktree cleanup failed: remove rejected for $verify_root" >&2
    exit 1
  fi
  if ! worktree_inventory="$(git worktree list --porcelain)"; then
    echo "worktree cleanup failed: registration inventory unavailable" >&2
    exit 1
  fi
  if [ -e "$verify_root" ] || grep -Fqx "worktree $verify_root" <<<"$worktree_inventory"; then
    echo "worktree cleanup failed: path or registration remains for $verify_root" >&2
    exit 1
  fi
  exit "$exit_status"
}

trap cleanup EXIT
git fetch origin "$base_ref"
git worktree add --detach "$verify_root" "$merge_oid"

export UV_CACHE_DIR=/private/tmp/uv-cache
uv build --directory "$verify_root"
uv run --locked --directory "$verify_root" pytest -q
uv run --locked --directory "$verify_root" ruff check src tests
uv run --locked --directory "$verify_root" pyright src/careerkit tests
uv run --locked --directory "$verify_root" lint-imports
uv run --locked --directory "$verify_root" \
  vulture src tests/static/vulture_whitelist.py --min-confidence 60
npm --prefix "$verify_root" ci
npm --prefix "$verify_root" run lint:js
```

Do not replace the worktree with `git archive` when tests require Git metadata.
The `EXIT` trap removes the registered worktree after success, failure, or interruption.

## Related Evidence

- `docs/retros/2026-08-02-saramin-screening-quality-guards-retro.md`
  records the stale-review race and the corrected merge verification.
- `docs/deviations/2026-08-03-pr42-review-hardening-002.md`
  records the first review-hardening contract.
- `docs/deviations/2026-08-03-pr42-review-hardening-003.md`
  supersedes its client merge boundary and hardens the fixture contracts.
- `docs/solutions/workflow-issues/release-loop-admission-gates-reject-unknown-or-drifting-plans.md`
  covers earlier phase and plan admission gates.
