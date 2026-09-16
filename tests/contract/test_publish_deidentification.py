
"""Rule contracts for the publish de-identification gate.

One positive test per rule, the closed-set equality over the *emitted* names, negative
tests for the aggregates, and a corpus regression over synthetic text. Every fixture
here is synthetic: no real organization name, record key, or posting sentence.
"""

from __future__ import annotations


def _guard_main_capturing(argv: list[str]) -> tuple[int, str]:
    """Run the CLI's main() capturing stderr, returning (exit code, stderr)."""
    import contextlib
    import io

    from careerkit.publish_guard import main

    buffer = io.StringIO()
    with contextlib.redirect_stderr(buffer):
        code = main(argv)
    return code, buffer.getvalue()




import importlib.util
import pathlib


import pytest

from careerkit.publish_guard import RULE_NAMES, Terms, normalize_text, scan_text




# Runtime-assembled carriers: the fixtures exercise the rules on strings built here so
# the guard's own repository text never carries a pattern in contiguous, committable form.
_LEGAL_FORM = "(" + "주" + ")"
_CACHE_NAME = "publish-guard-" + "terms.json"
_EMPLOYMENT = "합류"
def run(text: str, **kwargs: object) -> list[str]:
    terms = kwargs.pop("terms", None)
    found = scan_text(text, terms=terms, **kwargs)  # type: ignore[arg-type]
    return [finding.rule for finding in found]


def evidence(text: str, **kwargs: object) -> list[str]:
    terms = kwargs.pop("terms", None)
    found = scan_text(text, terms=terms, **kwargs)  # type: ignore[arg-type]
    return [finding.evidence for finding in found]


# --- Positive: one test per rule ---------------------------------------------


def test_private_path_matches_a_record_bearing_path() -> None:
    assert run("see private/jd/records/wanted/12345678/record.json") == ["private-path"]


def test_private_path_matches_a_home_anchored_path() -> None:
    assert run("saved under ~/work/private/jd/notes.md") == ["private-path"]


def test_private_path_does_not_match_the_bare_token() -> None:
    assert run("the private/ prefix appears in public documentation") == []


def test_record_key_matches_a_digit_key() -> None:
    assert run("wanted/12345678 processed") == ["record-key"]


def test_record_key_matches_a_slug_key() -> None:
    found = run("headhunter/some-company-slug-20260820 processed")
    assert found == ["record-key"]


def test_record_key_ignores_unknown_platforms() -> None:
    assert run("notaplatform/12345678") == []


def test_posting_url_matches_platform_urls() -> None:
    assert run("posting at https://www.wanted.co.kr/wd/12345678") == ["posting-url"]


def test_email_flags_real_addresses_but_not_exemptions() -> None:
    assert run("contact a.person+tag@company.io") == ["email"]
    assert run("noreply@github.com is exempt") == []
    assert run("someone@example.com is exempt") == []


def test_legal_suffix_matches_a_legal_form() -> None:
    assert run(f"founded as {_LEGAL_FORM} 테스트테크") == ["legal-suffix"]


def test_entity_suffix_flags_a_name_shaped_word() -> None:
    assert run("the 머큐리랩스 team shipped it") == ["entity-suffix"]


def test_entity_suffix_allows_generic_role_nouns() -> None:
    assert run("개발 팀이 그룹 미팅을 했다") == []


def test_entity_position_flags_the_recruitment_grammar() -> None:
    assert run("합류했습니다.") == ["entity-position"]


def test_entity_position_allows_generic_subjects() -> None:
    assert run("한 명의 개발자가 합류했습니다.") == []


def _init_repo(repo: pathlib.Path) -> None:
    import subprocess

    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True, capture_output=True)


def _git(repo: pathlib.Path, *argv: str) -> None:
    import subprocess

    subprocess.run(["git", *argv], cwd=repo, check=True, capture_output=True, text=True)




def test_cli_commits_range_names_paths_and_exempts_own_files(tmp_path, monkeypatch) -> None:
    """--commits scans added lines, names files, and exempts the guard's own files."""
    import subprocess

    repo = tmp_path
    module_dir = pathlib.Path(__file__).parents[2]
    own_source = (module_dir / "src" / "careerkit" / "publish_guard.py").read_text(
        encoding="utf-8"
    )
    def _git(*argv: str):
        subprocess.run(
            ["git", *argv], cwd=repo, check=True, capture_output=True, text=True
        )

    _git("init", "-q")
    _git("config", "commit.gpgsign", "false")
    _git("config", "user.email", "t@t")
    _git("config", "user.name", "t")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git("add", ".")
    _git("commit", "-m", "base")
    _git("checkout", "-q", "-b", "feature")
    (repo / "doc.md").write_text(f"정적 줄\n테크베이스와 {_EMPLOYMENT}했습니다\n", encoding="utf-8")
    own = repo / "src" / "careerkit"
    own.mkdir(parents=True)
    (own / "publish_guard.py").write_text(own_source, encoding="utf-8")
    (repo / "other.py").write_text(own_source, encoding="utf-8")
    (repo / "docs" / "publish_guard.py").parent.mkdir(exist_ok=True)
    (repo / "docs" / "publish_guard.py").write_text(own_source, encoding="utf-8")
    _git("add", ".")
    _git("commit", "-m", "carries patterns")
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD~1"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()

    monkeypatch.chdir(repo)
    monkeypatch.setenv("CAREER_WORKSPACE", "")
    code, stderr = _guard_main_capturing(
        ["--commits", f"{base_sha}..HEAD", "--layers", "1", "--no-evidence"]
    )
    assert code == 1, stderr[-400:]
    assert "doc.md:2" in stderr, stderr[-400:]
    assert "other.py:1" in stderr, stderr[-400:]
    assert "docs/publish_guard.py" in stderr, stderr[-400:]
    assert "src/careerkit/publish_guard.py" not in stderr, stderr[-400:]


def test_cli_staged_scans_a_quoted_nonascii_path(tmp_path, monkeypatch) -> None:
    """+++ headers that git quotes (core.quotePath) must still be parsed and scanned."""
    repo = tmp_path
    _init_repo(repo)
    (repo / "기존.md").write_text("clean line\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    # a Korean-named file carries a carrier; the +++ header will be quoted
    (repo / "문서.md").write_text(f"테크베이스와 {_EMPLOYMENT}했습니다\n", encoding="utf-8")
    _git(repo, "add", ".")
    monkeypatch.chdir(repo)
    code, stderr = _guard_main_capturing(["--staged", "--layers", "1", "--no-evidence"])
    assert code == 1, stderr[-300:]
    assert "문서.md:1" in stderr, stderr[-300:]
    assert "entity-position" in stderr, stderr[-300:]


def test_cli_range_scans_per_commit_not_net_diff(tmp_path, monkeypatch) -> None:
    """A line added and removed inside the same push must still fire on its commit."""
    repo = tmp_path
    _init_repo(repo)
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    (repo / "leak.txt").write_text("wanted/12345678 processed\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "add leak")
    (repo / "leak.txt").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "remove leak")
    monkeypatch.chdir(repo)
    code, stderr = _guard_main_capturing(["--commits", "HEAD~2..HEAD", "--layers", "1", "--no-evidence"])
    assert code == 1, stderr[-300:]


def test_cli_commits_unresolvable_head_exits_two(tmp_path, monkeypatch) -> None:
    repo = tmp_path
    _init_repo(repo)
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    monkeypatch.chdir(repo)
    code, stderr = _guard_main_capturing(["--commits", "HEAD~1..nosuchref", "--layers", "1", "--no-evidence"])
    assert code == 2, (code, stderr[-300:])
    code, stderr = _guard_main_capturing(["--commits", "deadbeefdeadbeef", "--exclude-remote", "origin", "--layers", "1", "--no-evidence"])
    assert code == 2, (code, stderr[-300:])


def test_cli_staged_skips_deletions_and_exempts_own_paths(tmp_path, monkeypatch) -> None:
    repo = tmp_path
    _init_repo(repo)
    (repo / "wanted/12345678.md").parent.mkdir()
    (repo / "wanted/12345678.md").write_text("x\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "add leak")
    (repo / "wanted/12345678.md").unlink()
    _git(repo, "add", "-A")
    monkeypatch.chdir(repo)
    code, stderr = _guard_main_capturing(["--staged", "--layers", "1", "--no-evidence"])
    assert code == 0, stderr[-300:]


def test_layer2_skips_when_the_base_ref_does_not_resolve(tmp_path, monkeypatch) -> None:
    """A live analyzer with an unresolvable base must skip layer 2, not flag every NNP."""
    from careerkit.publish_guard import _import_analyzer

    if _import_analyzer() is None:
        pytest.skip("analyzer not installed in this environment")

    repo = tmp_path
    _init_repo(repo)
    # no origin/main and no upstream: the resolver returns ''
    (repo / "m.txt").write_text(f"서울과 부산에서 모임을 했습니다\n", encoding="utf-8")
    monkeypatch.chdir(repo)
    code, stderr = _guard_main_capturing(["--message", str(repo / "m.txt"), "--layers", "1,2"])
    assert code == 0, stderr[-300:]
    assert "layer 2 base ref does not resolve" in stderr, stderr[-300:]
    assert "proper-noun" not in stderr, stderr[-300:]


def test_platform_names_are_the_reexported_known_set() -> None:
    from careerkit.jobs.application.storage_migration import _KNOWN_PLATFORMS
    from careerkit.publish_guard import platform_names

    assert platform_names() == frozenset(_KNOWN_PLATFORMS)


def test_record_key_matches_the_canonical_colon_form() -> None:
    assert run("wanted:12345678") == ["record-key"]
    assert run("wanted 12345678") == ["record-key"]
    assert run("wanted/12345678") == ["record-key"]


def test_fallback_scans_a_root_commit_paths(tmp_path, monkeypatch) -> None:
    """A fresh repo's first commit adds a path with a record-key-looking name."""
    repo = tmp_path
    _init_repo(repo)
    (repo / "wanted/12345678.md").parent.mkdir()
    (repo / "wanted/12345678.md").write_text("x\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "root")
    _git(repo, "remote", "add", "origin", "https://example.invalid/x.git")
    monkeypatch.chdir(repo)
    code, stderr = _guard_main_capturing(
        ["--commits", "HEAD", "--exclude-remote", "origin", "--layers", "1"]
    )
    assert code == 1, stderr[-400:]
    assert "wanted/12345678.md:0" in stderr, stderr[-400:]


def test_terms_cache_regeneration_preserves_the_exclusions(tmp_path, monkeypatch) -> None:
    """A cache rewrite must carry the reader's exclusion list through (writer rule)."""
    import json

    from careerkit.publish_guard import load_terms

    ws = tmp_path
    derived = ws / "private" / "jd" / "derived"
    derived.mkdir(parents=True)
    exclusions_file = derived / _CACHE_NAME
    exclusions_file.write_text(
        json.dumps({"generic_exclusions": ["테스트"]}, ensure_ascii=False), encoding="utf-8"
    )

    calls: list[object] = []

    class _FakeTerms:
        substring = frozenset({"어떤회사명"})
        bounded = frozenset({"테스트", "테스트사"})

    def _fake_generate(workspace):
        calls.append(workspace)
        return _FakeTerms()

    monkeypatch.setattr("careerkit.publish_guard.generate_terms", _fake_generate)
    monkeypatch.setattr("careerkit.publish_guard.terms_cache_key", lambda _ws: "key1")

    terms, _key = load_terms(ws)
    assert "테스트" not in terms.bounded, "exclusions must filter on generation"
    payload = json.loads(exclusions_file.read_text(encoding="utf-8"))
    assert payload["generic_exclusions"] == ["테스트"], "rewrite must preserve exclusions"

    # second read: cache hit path filters too, even if the cache predates the list
    payload2 = json.loads(exclusions_file.read_text(encoding="utf-8"))
    payload2["bounded"] = sorted(set(payload2["bounded"]) | {"테스트"})
    exclusions_file.write_text(json.dumps(payload2, ensure_ascii=False), encoding="utf-8")
    terms2, _ = load_terms(ws)
    assert "테스트" not in terms2.bounded, "cache read must filter exclusions"


def test_quoted_corpus_prose_consumes_short_code_spans() -> None:
    """A short inline code span closes its own pair; the next span starts fresh."""
    line = (
        "이 프로젝트는 `example/` 디렉토리에 예제 데이터를 제공합니다. "
        "개인 이력서를 만들려면 `private/profile/`과 `private/companies/` 디렉토리에 자신의 데이터를 생성해야 합니다."
    )
    assert run(line) == []


def test_entity_suffix_allows_a_generic_head_before_the_suffix_word() -> None:
    """The generic check reads the head (the part before the suffix word)."""
    assert run("search?query=예시랩스") == []
    assert run("search?query=테크베이스랩스") == ["entity-suffix"]


def test_quoted_corpus_prose_flags_a_quoted_sentence() -> None:
    assert run('the reply said "그 조직의 백엔드 팀에 입사했습니다" verbatim') == [
        "quoted-corpus-prose"
    ]


def test_quoted_corpus_prose_ignores_short_and_question_forms() -> None:
    assert run('a short quote "가나다" stays') == []
    assert run('a short quote "입사" stays') == []
    assert run('a question "그 팀에 언제 합류했나요?" stays') == []


def test_quoted_corpus_prose_ignores_quotes_without_an_employment_verb() -> None:
    """Ordinary quoted prose — README quotes, UI labels, interview examples — stays."""
    assert run('the doc quotes "확장 프로그램을 로드합니다" verbatim') == []
    assert run('an example "제가 모든 결정을 했습니다" in a guide') == []
    assert run('a quote "기술적으로 한 축을 담당했습니다" in an interview sheet') == []


def test_term_file_flags_the_cache_name() -> None:
    assert run(f"the cache lives at {_CACHE_NAME}") == ["term-file"]


def _real_analyzer():
    import importlib

    try:
        module = importlib.import_module("kiwipiepy")
    except ImportError:
        return None
    return getattr(module, "Kiwi")()


@pytest.mark.skipif(
    importlib.util.find_spec("kiwipiepy") is None,
    reason="analyzer extra not installed in this environment",
)
def test_layer2_with_the_real_analyzer_flags_an_unknown_proper_noun() -> None:
    """The shipped factory must produce an INSTANCE: _layer2 calls analyzer.tokenize."""
    from careerkit.publish_guard import _import_analyzer

    analyzer = _import_analyzer()
    assert analyzer is not None and hasattr(analyzer, "tokenize")
    findings = list(
        scan_text(
            "서울과 부산에서 모임을 했습니다",
            layers=frozenset({2}),
            analyzer=analyzer,
            vocabulary=frozenset(),
        )
    )
    assert [f.rule for f in findings] == ["proper-noun", "proper-noun"], findings
    assert {f.evidence for f in findings} == {"서울", "부산"}


def test_layer2_skips_silently_without_an_analyzer() -> None:
    from careerkit.publish_guard import _import_analyzer

    if _import_analyzer() is not None:
        pytest.skip("analyzer installed; the skip path is only reachable without it")
    findings = list(
        scan_text(
            "테크베이스와 협력했습니다",
            layers=frozenset({2}),
            analyzer=None,
            vocabulary=frozenset(),
        )
    )
    assert findings == []


# --- Layer 2 with a stub tagger ----------------------------------------------


class _Token:
    def __init__(self, form: str, tag: str) -> None:
        self.form = form
        self.tag = tag


class _StubTagger:
    def __init__(self, tokens: list[_Token]) -> None:
        self._tokens = tokens

    def tokenize(self, text: str) -> list[_Token]:
        return self._tokens


def test_layer2_flags_unknown_proper_nouns() -> None:
    analyzer = _StubTagger([_Token("테크베이스", "NNP")])
    found = scan_text(
        "테크베이스 합류", analyzer=analyzer, vocabulary=frozenset(), layers=frozenset({2})
    )
    assert [item.rule for item in found] == ["proper-noun"]


def test_layer2_subtracts_tracked_vocabulary() -> None:
    analyzer = _StubTagger([_Token("개발자", "NNP")])
    found = scan_text(
        "개발자 합류",
        analyzer=analyzer,
        vocabulary=frozenset({"개발자"}),
        layers=frozenset({2}),
    )
    assert list(found) == []


# --- Layer 3 ------------------------------------------------------------------


def _terms() -> Terms:
    return Terms(
        substring=frozenset({"테크베이스"}),
        bounded=frozenset({"테스트사"}),
    )


def test_layer3_r1_matches_substrings() -> None:
    assert run("테크베이스와 함께합니다", terms=_terms()) == ["local-term"]


def test_layer3_r2_matches_bounded_only() -> None:
    assert run("테스트사는 좋다", terms=_terms()) == ["local-term"]
    assert run("테스트사를 예시로 든다", terms=_terms()) == ["local-term"]
    assert run("낙엽테스트사라는 시집", terms=_terms()) == []


def test_normalization_decodes_and_folds() -> None:
    assert normalize_text("%EA%B0%80%EB%9D%BC") == "가라"
    assert normalize_text("테크\u00ad베이스") == "테크베이스"


def test_layer3_sees_percent_encoded_names() -> None:
    terms = Terms(substring=frozenset({"테크베이스"}), bounded=frozenset())
    assert run("we joined %ED%85%8C%ED%81%AC%EB%B2%A0%EC%9D%B4%EC%8A%A4", terms=terms) == [
        "local-term"
    ]


# --- Closed-set equality (S-3) ------------------------------------------------


def test_emitted_rules_cover_the_closed_set() -> None:
    positive_text = (
        "private/jd/records/wanted/12345678 x\n"
        "wanted/12345679 x\n"
        "https://www.wanted.co.kr/wd/12345678 x\n"
        "a.person+tag@company.io x\n"
        f"{_LEGAL_FORM} 테스트테크 x\n"
        "머큐리랩스 x\n"
        "합류했습니다. x\n"
        '"그 조직의 백엔드 팀에 입사했습니다" x\n'
        f"{_CACHE_NAME} x\n"
        "테크베이스와 함께 x\n"
        "테스트사는 좋다 x\n"
        "stub tagger covers proper-noun separately x\n"
    )
    terms = Terms(substring=frozenset({"테크베이스"}), bounded=frozenset({"테스트사"}))
    emitted = {
        rule
        for rule in run(positive_text, terms=terms)
    }
    # proper-noun is exercised with the stub tagger in its own test; the emitted union
    # over structural plus term rules must equal RULE_NAMES minus {proper-noun}.
    assert emitted == RULE_NAMES - {"proper-noun"}


def test_rule_names_is_the_eleven_name_set() -> None:
    assert RULE_NAMES == frozenset(
        {
            "private-path",
            "record-key",
            "posting-url",
            "email",
            "legal-suffix",
            "entity-suffix",
            "entity-position",
            "quoted-corpus-prose",
            "proper-noun",
            "local-term",
            "term-file",
        }
    )


def test_normalization_folds_halfwidth_jamo() -> None:
    """NFKC composes halfwidth jamo; NFC would leave it uncomposed (mutation target)."""
    assert normalize_text("\uffa1\uffc2") == "가"


def test_generator_splits_polluted_values_and_drops_short_fragments() -> None:
    """R1's split filter: a polluted value's fragment enters only at 4+ Hangul chars."""
    from careerkit.publish_guard import _build_terms

    terms = _build_terms(["테크 데이터베이스 백엔드 엔지니어 모집"])
    assert "테크" not in terms.substring
    assert any("데이터베이스" in term for term in terms.substring)


def test_platform_symbol_covers_the_store() -> None:
    from careerkit.publish_guard import platform_names

    names = platform_names()
    assert "wanted" in names
    assert "headhunter" in names
    assert "private" in names


# --- Aggregates: the repository's own text stays committable -------------------


def test_own_commit_trailers_pass() -> None:
    text = (
        "Constraint: an audit that prints a matched value would be the leak.\n"
        "Rejected: keep /tmp script and cite its output.\n"
        "Assisted-by: Claude with Claude Code\n"
    )
    assert run(text) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Measured over 2290 documents: 300 -> 295", []),
        ("the guard rejects; the author rephrases", []),
    ],
)
def test_aggregate_statistics_pass(text: str, expected: list[str]) -> None:
    assert run(text) == expected