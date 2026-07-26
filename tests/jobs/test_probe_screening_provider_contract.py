from __future__ import annotations

import json
from pathlib import Path

import pytest

from careerkit.jobs import probe_screening_provider_contract as probe
from careerkit.jobs.application.requirement_manifest import extract_requirement_manifest, without_main_duty

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "screening_contract"


class FakeOllamaProvider:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = list(outputs)
        self.calls = 0

    def run(
        self,
        prompt: str,
        timeout: int,
        local_timeout: int | None = None,
    ) -> tuple[str, str]:
        output = self.outputs[min(self.calls, len(self.outputs) - 1)]
        self.calls += 1
        return "ollama", output


def _build_valid_assessment_payload(
    fixture_paths: dict[str, Path] | None = None,
) -> str:
    paths = fixture_paths or probe.fixture_paths_from_root(_FIXTURE_DIR)
    manifest = without_main_duty(extract_requirement_manifest(paths["jd"].read_text(encoding="utf-8")))
    matches = [
        {
            "id": item.id,
            "match": "충족",
            "evidence": f"[source: private/profile/skills-job.md] {item.text} 근거",
        }
        for item in manifest.leaves
        if item.assessable
    ]
    return json.dumps(
        {
            "schema_version": 1,
            "matches": matches,
            "verdict": "지원 보류",
            "decision_basis": [
                item.id for item in manifest.parents if item.kind.value == "필수"
            ],
            "screening_summary": ["구조화 계약으로 평가를 완료했다"],
            "reasons": [
                "필수 요건 근거를 확인했다",
                "주요 업무 근거를 확인했다",
                "우대 요건 근거를 확인했다",
            ],
        },
        ensure_ascii=False,
    )


@pytest.fixture
def fixture_paths() -> dict[str, Path]:
    return {
        "jd": _FIXTURE_DIR / "jd.md",
        "candidate": _FIXTURE_DIR / "candidate.md",
        "company": _FIXTURE_DIR / "company.md",
        "rules": _FIXTURE_DIR / "rules.md",
    }


def test_probe_default_fixture_root_resolves_repository_fixtures() -> None:
    assert probe.DEFAULT_FIXTURE_ROOT == _FIXTURE_DIR
    assert all(path.is_file() for path in probe.fixture_paths_from_root(probe.DEFAULT_FIXTURE_ROOT).values())


def test_probe_accepts_three_valid_runs(fixture_paths) -> None:
    summary = probe.run_probe(
        provider="ollama",
        runs=3,
        fixture_paths=fixture_paths,
        provider_factory=lambda: FakeOllamaProvider([_build_valid_assessment_payload()]),
    )

    assert summary == {"runs": 3, "accepted": 3, "retried": 0, "failed": 0}


def test_probe_main_returns_zero_for_three_valid_runs(capsys) -> None:
    exit_code = probe.main(
        [
            "--provider",
            "ollama",
            "--runs",
            "3",
            "--fixture-root",
            str(_FIXTURE_DIR),
        ],
        provider_factory=lambda: FakeOllamaProvider(
            [_build_valid_assessment_payload()]
        ),
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {
        "runs": 3,
        "accepted": 3,
        "retried": 0,
        "failed": 0,
    }


def test_probe_counts_retry_when_first_response_is_invalid(fixture_paths) -> None:
    summary = probe.run_probe(
        provider="ollama",
        runs=1,
        fixture_paths=fixture_paths,
        provider_factory=lambda: FakeOllamaProvider(
            ["{}", _build_valid_assessment_payload()]
        ),
    )

    assert summary == {"runs": 1, "accepted": 1, "retried": 1, "failed": 0}


def test_probe_counts_retry_when_both_responses_are_invalid(fixture_paths) -> None:
    summary = probe.run_probe(
        provider="ollama",
        runs=1,
        fixture_paths=fixture_paths,
        provider_factory=lambda: FakeOllamaProvider(["{}", "{}"]),
    )

    assert summary == {"runs": 1, "accepted": 0, "retried": 1, "failed": 1}


def test_probe_returns_nonzero_when_fewer_than_two_runs_are_accepted(fixture_paths, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = probe.main(
        [
            "--provider",
            "ollama",
            "--runs",
            "3",
            "--fixture-root",
            str(_FIXTURE_DIR),
        ],
        provider_factory=lambda: FakeOllamaProvider(["{}", "{}"]),
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 1
    assert payload == {"runs": 3, "accepted": 0, "retried": 3, "failed": 3}


@pytest.mark.parametrize(
    "args",
    (
        ["--runs", "2"],
        ["--runs", "0"],
        ["--timeout", "0"],
    ),
)
def test_probe_rejects_unapproved_or_nonpositive_numeric_options(args) -> None:
    with pytest.raises(SystemExit, match="2"):
        probe.parse_args(args)


def test_probe_run_does_not_write_screening_records(fixture_paths) -> None:
    result = probe.run_probe_once(
        provider_factory=lambda: FakeOllamaProvider([_build_valid_assessment_payload()]),
        fixture_paths=fixture_paths,
        run_index=1,
        provider="ollama",
    )

    assert result.accepted is True
    assert result.retried is False
    assert result.has_screening is False
