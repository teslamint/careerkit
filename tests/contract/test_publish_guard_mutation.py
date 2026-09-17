"""Predicate-mutation proofs for the publish de-identification gate.

Each proof rewrites the shipped module's source with one predicate weakened, writes it
to a scratch package, and runs the contract suite against that mutant. A rule whose
contract tests pass with its predicate weakened is decorative; the assertions below are
what prove each rule is not.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "src" / "careerkit" / "publish_guard.py"
CONTRACT = "tests/contract/test_publish_deidentification.py"


def run_suite(mutated_source: str, contract: str = CONTRACT) -> tuple[int, str, list[str]]:
    """Run the contract suite against a mutated module copy.

    The mutant is placed in a scratch tree that shadows ``src`` on PYTHONPATH, so the
    suite's ``from careerkit.publish_guard import ...`` resolves to the mutant. The
    environment is scrubbed of CAREER_WORKSPACE the way the full suite runs.
    """
    with tempfile.TemporaryDirectory() as tmp:
        pkg = Path(tmp)
        shutil.copytree(ROOT / "src" / "careerkit", pkg / "careerkit",
                        ignore=shutil.ignore_patterns("__pycache__"))
        (pkg / "careerkit" / "publish_guard.py").write_text(mutated_source, encoding="utf-8")
        env = dict(os.environ)
        env["PYTHONPATH"] = f"{tmp}{os.pathsep}{env.get('PYTHONPATH', '')}"
        env["CAREER_WORKSPACE"] = ""
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "--no-header",
             "-p", "no:cacheprovider", contract],
            capture_output=True, check=False, text=True,
            env=env, cwd=str(ROOT),
        )
        failed = [
            line.split(" ")[1].removeprefix(contract.split("::")[0] + "::")
            for line in (result.stdout + result.stderr).splitlines()
            if line.startswith("FAILED ")
        ]
        return result.returncode, result.stdout + result.stderr, failed


def assert_mutation_breaks_named_tests(anchor: str, replacement: str, named: list[str]) -> None:
    """Apply one predicate mutation and prove exactly the named contract tests fail."""
    source = MODULE_SOURCE
    assert anchor in source, f"mutation anchor missing: {anchor[:70]}"
    mutant = source.replace(anchor, replacement, 1)
    assert mutant != source
    _code, output, failed = run_suite(mutant)
    for test in named:
        assert test in failed, (
            f"mutation '{anchor[:40]}...' did not break {test}; "
            f"the predicate's contract is decorative.\nOutput tail: {output[-400:]}"
        )


MODULE_SOURCE = (ROOT / "src" / "careerkit" / "publish_guard.py").read_text(encoding="utf-8")


def test_private_path_widened_to_the_bare_token_breaks_its_negative_test() -> None:
    assert_mutation_breaks_named_tests(
        'r"(?:~|/Users/|/home/)[^\\s]*private(?:/|\\b)|private/jd/records/[a-z]+/[^\\s/]+"',
        'r"private/"',
        ["test_private_path_does_not_match_the_bare_token"],
    )


def test_email_losing_its_exemption_breaks_the_exemption_test() -> None:
    assert_mutation_breaks_named_tests(
        r'_EMAIL_EXEMPT = re.compile(r"^(?:noreply@|[^@]*@example\.)", re.IGNORECASE)',
        "_EMAIL_EXEMPT = re.compile(r'(?!)')",
        ["test_email_flags_real_addresses_but_not_exemptions"],
    )


def test_merge_base_replaced_by_head_breaks_the_divergence_proof() -> None:
    """A resolver mutant that returns HEAD must fail the divergence proof."""
    source = MODULE_SOURCE
    mutant = source.replace(
        '        base = git("merge-base", "HEAD", upstream)',
        '        base = git("rev-parse", "HEAD")',
        1,
    )
    assert mutant != source
    _code, output, failed = run_suite(
        mutant, "tests/contract/test_publish_guard_mutation.py::test_layer2_base_ref_replaces_head_is_observable"
    )
    assert "test_layer2_base_ref_replaces_head_is_observable" in failed, output[-400:]


def test_platform_names_narrowed_to_a_subset_breaks_the_equality_test() -> None:
    """A hand-kept subset must fail the re-export equality test, not only crash."""
    source = MODULE_SOURCE
    mutant = source.replace(
        "    return frozenset(_KNOWN_PLATFORMS)",
        '    return frozenset({"wanted", "headhunter", "private"})',
        1,
    )
    assert mutant != source
    _code, output, failed = run_suite(mutant)
    assert "test_platform_names_are_the_reexported_known_set" in failed, output[-400:]


def test_record_key_reading_the_adapters_set_breaks_the_slug_test() -> None:
    assert_mutation_breaks_named_tests(
        "from careerkit.jobs.application.storage_migration import _KNOWN_PLATFORMS",
        "raise ImportError('mutant: platform set unavailable')",
        ["test_record_key_matches_a_slug_key"],
    )


def test_r2_substring_matching_breaks_the_bounded_negative_test() -> None:
    assert_mutation_breaks_named_tests(
        "    for term in terms.bounded:\n"
        "        if re.search(_R2_BOUND + re.escape(term) + _R2_TRAIL, text):",
        "    for term in terms.bounded:\n"
        "        if term in text:",
        ["test_layer3_r2_matches_bounded_only"],
    )


def test_r1_admitting_short_fragments_breaks_the_fragment_test() -> None:
    assert_mutation_breaks_named_tests(
        "                    and len(piece) >= 4\n",
        "                    and len(piece) >= 2\n",
        ["test_generator_splits_polluted_values_and_drops_short_fragments"],
    )


def test_quoted_prose_min_length_dropped_breaks_the_short_test() -> None:
    """Dropping the code-side 12-char minimum lets the short-form test fail."""
    source = MODULE_SOURCE
    mutant = source.replace(
        "            len(fragment) >= 12\n",
        "            len(fragment) >= 1\n",
        1,
    )
    assert mutant != source
    _code, output, failed = run_suite(mutant)
    assert "test_quoted_corpus_prose_ignores_short_and_question_forms" in failed, output[-400:]


def test_normalization_reverted_to_nfc_breaks_the_jamo_test() -> None:
    assert_mutation_breaks_named_tests(
        '    normalized = unicodedata.normalize("NFKC", decoded)',
        '    normalized = unicodedata.normalize("NFC", decoded)',
        ["test_normalization_folds_halfwidth_jamo"],
    )


def test_layer2_base_ref_replaces_head_is_observable(tmp_path, monkeypatch) -> None:
    """Replacing the merge-base ref with HEAD makes an amend subtract the amended name."""
    import subprocess

    def _git(*argv: str):
        return subprocess.run(["git", *argv], cwd=tmp_path, check=True,
                              capture_output=True, text=True)

    _git("init", "-q", "-b", "main")
    _git("config", "commit.gpgsign", "false")
    _git("config", "user.email", "t@t")
    _git("config", "user.name", "t")
    (tmp_path / "base.txt").write_text("base\n", encoding="utf-8")
    _git("add", ".")
    _git("commit", "-m", "base")
    base_sha = _git("rev-parse", "HEAD").stdout.strip()
    _git("checkout", "-q", "-b", "feature")
    (tmp_path / "work.txt").write_text("work\n", encoding="utf-8")
    _git("add", ".")
    _git("commit", "-m", "work")
    head_sha = _git("rev-parse", "HEAD").stdout.strip()
    # A fake origin whose main points at the base commit; the feature branch tracks it
    # so the shipped resolver resolves a genuine merge base rather than HEAD.
    _git("remote", "add", "origin", "https://example.invalid/x.git")
    _git("update-ref", "refs/remotes/origin/main", base_sha)
    _git("config", "branch.feature.remote", "origin")
    _git("config", "branch.feature.merge", "refs/heads/main")
    # @{push} must resolve to origin/main so the resolver's primary branch fires; the
    # origin/main fallback below it covers the same resolution deterministically.
    _git("config", "push.default", "upstream")

    monkeypatch.chdir(tmp_path)
    from careerkit.publish_guard import _merge_base_ref

    base = _merge_base_ref()
    assert base == base_sha, "the shipped predicate must resolve the merge base"
    assert base != head_sha, "the base must differ from HEAD on diverged branches"
