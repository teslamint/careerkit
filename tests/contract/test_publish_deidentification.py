"""Rule contracts for the publish de-identification gate.

One positive test per rule, the closed-set equality over the *emitted* names, negative
tests for the aggregates, and a corpus regression over synthetic text. Every fixture
here is synthetic: no real organization name, record key, or posting sentence.
"""

from __future__ import annotations


import pytest

from careerkit.publish_guard import RULE_NAMES, Terms, normalize_text, scan_text


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
    assert run("founded as (주) 가온테크") == ["legal-suffix"]


def test_entity_suffix_flags_a_name_shaped_word() -> None:
    assert run("the 머큐리랩스 team shipped it") == ["entity-suffix"]


def test_entity_suffix_allows_generic_role_nouns() -> None:
    assert run("개발 팀이 그룹 미팅을 했다") == []


def test_entity_position_flags_the_recruitment_grammar() -> None:
    assert run("합류했습니다.") == ["entity-position"]


def test_entity_position_allows_generic_subjects() -> None:
    assert run("한 명의 개발자가 합류했습니다.") == []


def test_quoted_corpus_prose_flags_a_quoted_sentence() -> None:
    assert run('the reply said "그 조직의 백엔드 팀에 입사했습니다" verbatim') == [
        "quoted-corpus-prose"
    ]


def test_quoted_corpus_prose_ignores_short_and_question_forms() -> None:
    assert run('a short quote "가나다" stays') == []
    assert run('a short quote "입사" stays') == []
    assert run('a question "몇 명이 합류했나요?" stays') == []


def test_term_file_flags_the_cache_name() -> None:
    assert run("the cache lives at publish-guard-terms.json") == ["term-file"]


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
        substring=frozenset({"가온물산"}),
        bounded=frozenset({"가온"}),
    )


def test_layer3_r1_matches_substrings() -> None:
    assert run("가온물산과 함께합니다", terms=_terms()) == ["local-term"]


def test_layer3_r2_matches_bounded_only() -> None:
    assert run("가온은 좋다", terms=_terms()) == ["local-term"]
    assert run("가온을 예시로 든다", terms=_terms()) == ["local-term"]
    assert run("낙엽가온이라는 시집", terms=_terms()) == []


def test_normalization_decodes_and_folds() -> None:
    assert normalize_text("%EA%B0%80%EC%98%A8") == "가온"
    assert normalize_text("가온\u00ad물산") == "가온물산"


def test_layer3_sees_percent_encoded_names() -> None:
    terms = Terms(substring=frozenset({"가온물산"}), bounded=frozenset())
    assert run("we joined %EA%B0%80%EC%98%A8%EB%AC%BC%EC%82%B0", terms=terms) == [
        "local-term"
    ]


# --- Closed-set equality (S-3) ------------------------------------------------


def test_emitted_rules_cover_the_closed_set() -> None:
    positive_text = (
        "private/jd/records/wanted/12345678 x\n"
        "wanted/12345679 x\n"
        "https://www.wanted.co.kr/wd/12345678 x\n"
        "a.person+tag@company.io x\n"
        "(주) 가온테크 x\n"
        "머큐리랩스 x\n"
        "합류했습니다. x\n"
        '"그 조직의 백엔드 팀에 입사했습니다" x\n'
        "publish-guard-terms.json x\n"
        "가온물산과 함께 x\n"
        "가온은 좋다 x\n"
        "stub tagger covers proper-noun separately x\n"
    )
    terms = Terms(substring=frozenset({"가온물산"}), bounded=frozenset({"가온"}))
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

    terms = _build_terms(["가온 데이터베이스 백엔드 엔지니어 모집"])
    assert "가온" not in terms.substring
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