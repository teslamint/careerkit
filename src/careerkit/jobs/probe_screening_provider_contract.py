from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Callable, NamedTuple

from careerkit.jobs.adapters.screening.cli_provider import (
    LLMProvider,
    resolve_local_llm,
    run_local_llm,
)
from careerkit.jobs.adapters.storage.file_records import JDRecordRepository
from careerkit.jobs.application.screening import run_screening
from careerkit.jobs.domain.model import JobRecord
from careerkit.workspace import resolve_workspace


DEFAULT_FIXTURE_ROOT = (
    Path(__file__).resolve().parents[3]
    / "tests"
    / "jobs"
    / "fixtures"
    / "screening_contract"
)
DEFAULT_RUNS = 3
DEFAULT_TIMEOUT = 120


def _three_runs(raw: str) -> int:
    value = int(raw)
    if value != DEFAULT_RUNS:
        raise argparse.ArgumentTypeError(f"value must be {DEFAULT_RUNS}")
    return value


def _positive_int(raw: str) -> int:
    value = int(raw)
    if value < 1:
        raise argparse.ArgumentTypeError("value must be at least 1")
    return value


class ProbeRunResult(NamedTuple):
    accepted: bool
    retried: bool
    has_screening: bool
    error: str | None


class OllamaOnlyProvider:
    def __init__(self) -> None:
        self.calls = 0

    def run(
        self,
        prompt: str,
        timeout: int,
        local_timeout: int | None = None,
    ) -> tuple[str, str]:
        self.calls += 1
        local = resolve_local_llm(dict(os.environ))
        if local is None:
            raise RuntimeError("ollama: local provider is disabled")
        label, url, model, extra_options = local
        if label != "ollama":
            raise RuntimeError(f"{label}: only ollama provider is allowed")
        if not model:
            raise RuntimeError("ollama: OLLAMA_SCREENING_MODEL not set")
        output, _prompt_tokens = run_local_llm(
            label, url, model, extra_options, prompt, local_timeout if local_timeout is not None else timeout
        )
        return label, output


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=("ollama",), default="ollama")
    parser.add_argument("--runs", type=_three_runs, default=DEFAULT_RUNS)
    parser.add_argument("--timeout", type=_positive_int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--fixture-root", type=Path, default=DEFAULT_FIXTURE_ROOT)
    return parser.parse_args(argv)


def fixture_paths_from_root(root: Path) -> dict[str, Path]:
    return {
        "jd": root / "jd.md",
        "candidate": root / "candidate.md",
        "company": root / "company.md",
        "rules": root / "rules.md",
    }


def _generic_job_record(run_index: int) -> JobRecord:
    job_id = f"probe-{run_index:03d}"
    return JobRecord(
        platform="wanted",
        job_id=job_id,
        company="Example Company",
        position="Backend Engineer",
        source_url=f"https://example.com/jobs/{job_id}",
    )


def _prepare_workspace(temp_root: Path, fixture_paths: dict[str, Path]) -> tuple[Path, Path]:
    (temp_root / ".career-workspace").write_text("1", encoding="utf-8")
    (temp_root / "private/jd/config").mkdir(parents=True, exist_ok=True)
    company_path = temp_root / "private/company_info/example-company.md"
    company_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(fixture_paths["company"], company_path)
    shutil.copyfile(
        fixture_paths["rules"],
        temp_root / "private/jd/config/jd-screening-rules.md",
    )
    return company_path, temp_root / "private/jd/records"


def run_probe_once(
    *,
    provider_factory: Callable[[], LLMProvider],
    fixture_paths: dict[str, Path],
    run_index: int,
    provider: str,
    timeout: int = DEFAULT_TIMEOUT,
) -> ProbeRunResult:
    if provider != "ollama":
        raise ValueError(f"Unsupported provider: {provider}")

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_root = Path(temp_dir)
        company_path, records_root = _prepare_workspace(temp_root, fixture_paths)
        workspace = resolve_workspace(explicit=temp_root)
        repository = JDRecordRepository(records_root)
        record = _generic_job_record(run_index)
        stored = repository.create(
            record,
            jd_markdown=fixture_paths["jd"].read_text(encoding="utf-8"),
        )
        provider_instance = provider_factory()

        try:
            result = run_screening(
                workspace=workspace,
                jd=stored,
                company_file=company_path,
                llm_timeout=timeout,
                dry_run=True,
                llm_provider=provider_instance,
                repository=repository,
                candidate_context=fixture_paths["candidate"].read_text(encoding="utf-8"),
            )
            accepted = result.used_fallback is False
            error = None
        except Exception as exc:
            accepted = False
            error = str(exc)

        metadata = repository.get_metadata(record.key)
        calls = int(getattr(provider_instance, "calls", 0))
        return ProbeRunResult(
            accepted=accepted,
            retried=calls > 1,
            has_screening=metadata.has_screening,
            error=error,
        )


def run_probe(
    *,
    provider: str,
    runs: int,
    fixture_paths: dict[str, Path],
    provider_factory: Callable[[], LLMProvider] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, int]:
    if runs <= 0:
        raise ValueError("runs must be positive")

    factory = provider_factory or OllamaOnlyProvider
    accepted = 0
    retried = 0
    failed = 0
    for run_index in range(1, runs + 1):
        result = run_probe_once(
            provider_factory=factory,
            fixture_paths=fixture_paths,
            run_index=run_index,
            provider=provider,
            timeout=timeout,
        )
        if result.has_screening:
            raise RuntimeError("probe must not publish screening records")
        retried += int(result.retried)
        if result.accepted:
            accepted += 1
        else:
            failed += 1
    return {"runs": runs, "accepted": accepted, "retried": retried, "failed": failed}


def main(
    argv: list[str] | None = None,
    *,
    provider_factory: Callable[[], LLMProvider] | None = None,
) -> int:
    args = parse_args(argv)
    summary = run_probe(
        provider=args.provider,
        runs=args.runs,
        fixture_paths=fixture_paths_from_root(args.fixture_root),
        provider_factory=provider_factory,
        timeout=args.timeout,
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["accepted"] >= 2 else 1


if __name__ == "__main__":
    raise SystemExit(main())
