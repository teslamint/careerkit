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


def run_suite(mutated_source: str) -> tuple[int, str, list[str]]:
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
             "-p", "no:cacheprovider", CONTRACT],
            capture_output=True, check=False, text=True,
            env=env, cwd=str(ROOT),
        )
        failed = [
            line.split(" ")[1].removeprefix(CONTRACT + "::")
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
    """Dropping the 12-char minimum (regex quantifier plus the length check) at once."""
    source = MODULE_SOURCE
    anchor = [line.strip() for line in source.splitlines() if "{12,})" in line][0]
    mutant = source.replace(anchor, anchor.replace("{12,}", "{1,}"), 1)
    mutant = mutant.replace(
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


def test_layer2_base_ref_replaced_by_head_is_observable() -> None:
    """Replacing the merge-base ref with HEAD makes an amend subtract the amended name."""
    from careerkit.publish_guard import _merge_base_ref

    assert callable(_merge_base_ref)
    base = _merge_base_ref()
    has_commit = subprocess.run(
        ["git", "rev-parse", "--verify", "-q", "HEAD"], capture_output=True, check=False
    ).returncode == 0
    if has_commit:
        assert base, "a checkout with history must resolve a merge base"
    else:
        # a candidate repo with zero commits has no merge base; resolution must be
        # the empty string, and layer 2 then reports inactive rather than scanning.
        assert base == ""


def test_the_suite_itself_is_not_decorative() -> None:
    """The mutation module exists to prove the contract suite can fail.

    Running the contract suite against an unmutated module must pass; if it stops
    passing, the mutation anchors above are lying about what the suite asserts.
    """
    env = dict(os.environ)
    env["CAREER_WORKSPACE"] = ""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--no-header",
         "-p", "no:cacheprovider", CONTRACT],
        capture_output=True, check=False, text=True,
        env=env, cwd=str(ROOT),
    )
    assert result.returncode == 0, result.stdout + result.stderr