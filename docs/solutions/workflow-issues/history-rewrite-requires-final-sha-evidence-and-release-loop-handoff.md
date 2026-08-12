---
module: release-loop
date: "2026-08-05"
problem_type: workflow_issue
component: final-sha-evidence
severity: high
applies_when:
  - "a squash, rebase, or history rewrite changes reviewed commit lineage"
  - "fixture evidence was captured before the final ship SHA existed"
  - "shipping runs from an isolated Git worktree"
  - "the isolated worktree will be removed after merge"
related_components:
  - review-evidence
  - fixture-evidence
  - isolated-worktree
  - release-loop-state
tags:
  - release-loop
  - final-sha
  - history-rewrite
  - evidence-replay
  - isolated-worktree
  - handoff
---

# History rewrites require final-SHA evidence and release-loop handoff

## Context

코드와 테스트가 green이어도 retained evidence가 이전 commit을 가리킬 수 있다.
Fix commit, squash, rebase는 review 대상 SHA를 바꾸지만 evidence 파일은 자동으로 바뀌지 않는다.

이 상태에서는 mutation record, matrix summary, forced-failure 결과가 현재 PR을 증명하지 못한다.
또한 isolated worktree를 제거하면 gitignored `.release-loop` 기록도 함께 사라질 수 있다.

## Guidance

1. History rewrite 뒤 `git rev-parse HEAD`로 final SHA를 다시 확인한다.
2. Mutation record와 matrix summary를 final SHA에서 다시 실행한다.
3. Evidence마다 source SHA, exact command, setup, result를 기록한다.
4. `--basetemp` 부모 경로를 명시적으로 생성하고 기록한다.
5. Success뿐 아니라 failure, rollback, compensation, cancellation도 다시 실행한다.
6. Remote merge와 local cleanup을 별도 상태로 검증한다.
7. Worktree 제거 전에 `.release-loop` 전체를 지속할 위치로 옮긴다.

## Why This Matters

Stale SHA evidence는 다른 코드를 검증한 기록이다.
Green code는 현재 behavior를 보여주지만 이전 evidence의 provenance를 고치지 않는다.

Merge 명령도 두 상태를 포함할 수 있다.
Remote merge는 성공했지만 local checkout 또는 branch cleanup은 실패할 수 있다.
Remote state를 확인하지 않고 재시도하면 이미 완료된 mutation을 오판할 수 있다.

## When to Apply

- fix commit 뒤 squash 또는 rebase를 실행했을 때
- reviewer가 stale evidence 또는 wrong SHA를 지적했을 때
- mutation matrix가 failure와 recovery behavior를 요구할 때
- release-loop가 isolated worktree에서 실행될 때
- merge 뒤 worktree와 feature branch를 삭제할 때

## Examples

Final SHA에서 evidence를 재생성한다.

```bash
final_sha="$(git rev-parse HEAD)"
mkdir -p /tmp/feature-evidence/owned
uv run pytest -q \
  --basetemp=/tmp/feature-evidence/owned \
  tests/path/test_feature.py
printf 'source_commit: %s\n' "$final_sha" >> evidence/verification.txt
```

Failure contract도 별도로 실행한다.

```bash
uv run pytest -q \
  --basetemp=/tmp/feature-evidence/contracts \
  tests/path/test_feature.py \
  -k 'forced_failure or rollback or cancellation'
```

Merge 결과를 확인한 뒤 lifecycle state를 handoff한다.

```bash
gh pr view <pr-number> --json state,mergeCommit
cp -R .release-loop /path/that-survives-worktree-removal
git worktree remove <worktree-path>
```

Rule of thumb:

- Green tests confirm code state.
- Final-SHA replay confirms evidence state.
- Successful worktree removal confirms cleanup state.
