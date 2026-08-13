from __future__ import annotations

from pathlib import Path

import pytest

from careerkit.jobs.adapters.storage.file_records import StoredJobRecord
from careerkit.jobs.application.evidence_checks import (
    EvidenceReport,
    apply_demotions,
    check_rows,
    corpus_basenames,
    corpus_source_paths,
    extract_tokens,
    parse_match_table,
    resolve_source_paths,
)
from careerkit.jobs.application.requirement_manifest import extract_requirement_manifest
from careerkit.jobs.application.screening import build_fallback_output, validate_screening_structure
from careerkit.jobs.application.screening_assessment import (
    parse_screening_assessment,
    render_screening_markdown,
)
from careerkit.jobs.domain.model import JobRecord

CORPUS = """[source: private/profile/skills-job.md]
Spring Boot, Kotlin, FastAPI, NestJS, Laravel, Vue.js, MySQL, MariaDB, Redis, RabbitMQ,
JPA, QueryDSL, Sequelize, SQLAlchemy, Docker, AWS, Terraform, GraphQL

[source: private/companies/examplecorp/projects/chatapp.md]
Unit of Work 패턴으로 트랜잭션 경계를 명확히 하고 분산 락으로 동시성을 제어했다.
"""


def table(*rows: str) -> str:
    body = "\n".join(rows)
    return f"""## 기본 정보

| 항목 | 내용 |
|---|---|
| 회사명 | 테스트 |

## 이력/경험 매칭

| 요건 | 구분 | 대조 | 근거 |
|---|---|---|---|
{body}

## 최종 판정

### 최종 판정: 지원 추천
"""


def test_parses_four_column_table():
    rows, error = parse_match_table(
        table(
            "| Spring Boot 개발 | 필수 | 충족 | 다수 경험 |",
            "| Terraform 운영 | 우대 | 부분 | 일부 |",
            "| Kafka 파이프라인 | 필수 | 없음 | 확인되지 않음 |",
        )
    )
    assert error == ""
    assert [(r.index, r.kind, r.match) for r in rows] == [
        (0, "필수", "충족"),
        (1, "우대", "부분"),
        (2, "필수", "없음"),
    ]
    assert rows[0].requirement == "Spring Boot 개발"
    assert rows[0].evidence == "다수 경험"


def test_existing_four_column_document_without_provenance_metadata_stays_valid():
    assert validate_screening_structure(
        """## 기본 정보

| 항목 | 내용 |
|---|---|
| 회사명 | 테스트 |

## 스크리닝 결과

- 구조화된 4열 문서

## 이력/경험 매칭

| 요건 | 구분 | 대조 | 근거 |
|---|---|---|---|
| Spring Boot 개발 | 필수 | 충족 | 사내 백엔드 개발 경험 |

## 최종 판정

### 최종 판정: 지원 추천

## 핵심 근거

- 기존 4열 계약 유지
"""
    ) == (True, "")


def test_header_only_four_column_table_reports_no_rows():
    rows, error = parse_match_table(
        "".join(table("| 요건 | 구분 | 대조 | 근거 |"))
    )

    assert rows == []
    assert error == "매칭 표 행 없음"


def test_accepts_main_duty_kind():
    rows, error = parse_match_table("".join(table("| RAG 구축 | 주요업무 | 없음 | 없음 |")))
    assert error == ""
    assert rows[0].kind == "주요업무"


def test_escaped_pipe_in_a_requirement_is_not_a_column_break():
    rows, error = parse_match_table(
        table(r"| Java \| Kotlin 개발 | 필수 | 충족 | 다수 경험 |")
    )
    assert error == ""
    assert rows[0].requirement == r"Java \| Kotlin 개발"
    assert (rows[0].kind, rows[0].match) == ("필수", "충족")


def test_demotion_targets_the_right_cell_when_a_requirement_escapes_a_pipe():
    markdown = table(r"| Java \| Kotlin 개발 | 필수 | 충족 | 주장 |")
    rows, _ = parse_match_table(markdown)
    updated = apply_demotions(markdown, rows, (0,))
    reparsed, error = parse_match_table(updated)
    assert error == ""
    assert reparsed[0].requirement == r"Java \| Kotlin 개발"
    assert reparsed[0].match == "없음"
    assert reparsed[0].evidence == "주장"


def test_rejects_symbol_match_value():
    _, error = parse_match_table(table("| Spring Boot | 필수 | ⭕ | 근거 |"))
    assert error == "대조 칸 허용 밖 값: ⭕"


def test_rejects_legacy_prose_match_value():
    _, error = parse_match_table(table("| Spring Boot | 필수 | 직접 대응 | 근거 |"))
    assert error == "대조 칸 허용 밖 값: 직접 대응"


def test_rejects_a_row_with_a_fifth_column():
    """Only cells[3] is read as evidence, so a citation shifted past it would
    escape the source check — reject the row instead of accepting it truncated."""
    _, error = parse_match_table(
        table("| Spring Boot | 필수 | 충족 | 근거 | [source: private/x.md] |")
    )
    assert error == "매칭 표 컬럼 초과"


def test_rejects_unknown_kind_value():
    _, error = parse_match_table(table("| Spring Boot | 담당업무 | 충족 | 근거 |"))
    assert error == "구분 칸 허용 밖 값: 담당업무"


def test_a_five_column_header_cannot_shape_the_table():
    """A header is a row too: skipping it before the count checks would let a
    malformed header publish over four-column data rows."""
    markdown = """## 이력/경험 매칭

| 요건 | 구분 | 대조 | 근거 | 비고 |
|---|---|---|---|---|
| Spring Boot | 필수 | 충족 | 근거 |

## 최종 판정
"""
    _, error = parse_match_table(markdown)
    assert error == "매칭 표 컬럼 초과"


def test_a_two_column_header_cannot_shape_the_table():
    markdown = """## 이력/경험 매칭

| 요건 | 내용 |
|---|---|
| Spring Boot | 필수 | 충족 | 근거 |

## 최종 판정
"""
    _, error = parse_match_table(markdown)
    assert error == "매칭 표 컬럼 부족"


def test_rejects_three_column_table():
    markdown = """## 이력/경험 매칭

| 요건 | 대조 결과 | 근거 |
|---|---|---|
| Spring Boot | 직접 대응 | 있음 |

## 최종 판정
"""
    _, error = parse_match_table(markdown)
    assert error == "매칭 표 컬럼 부족"


def test_missing_heading_reports_absence():
    _, error = parse_match_table("## 기본 정보\n\n내용\n")
    assert error == "매칭 표 없음"


def test_empty_table_reports_no_rows():
    markdown = """## 이력/경험 매칭

| 요건 | 구분 | 대조 | 근거 |
|---|---|---|---|

    ## 최종 판정
    """
    rows, error = parse_match_table(markdown)
    assert error == "매칭 표 행 없음"
    assert rows == []


def test_non_table_prose_in_the_match_section_is_rejected():
    """A gap described in prose instead of a row is invisible to every check
    that reads rows — the contract confines this section to the table."""
    markdown = table("| Spring Boot 개발 | 필수 | 충족 | 다수 경험 |").replace(
        "\n## 최종 판정", "\n미충족 항목: Kafka 스트리밍 경험 없음\n\n## 최종 판정"
    )
    _, error = parse_match_table(markdown)
    assert error == "매칭 표 밖 내용: 미충족 항목: Kafka 스트리밍 경험 없음"


def test_a_sub_heading_in_the_match_section_is_rejected():
    markdown = table("| Spring Boot 개발 | 필수 | 충족 | 다수 경험 |").replace(
        "\n## 최종 판정", "\n### 자격요건 대조\n\n## 최종 판정"
    )
    _, error = parse_match_table(markdown)
    assert error == "매칭 표 밖 내용: ### 자격요건 대조"


def test_a_horizontal_rule_in_the_match_section_is_tolerated():
    markdown = table("| Spring Boot 개발 | 필수 | 충족 | 다수 경험 |").replace(
        "\n## 최종 판정", "\n---\n\n## 최종 판정"
    )
    rows, error = parse_match_table(markdown)
    assert error == ""
    assert len(rows) == 1


def test_fallback_two_column_document_is_exempt_from_the_four_column_reader():
    stored = StoredJobRecord(
        record=JobRecord("wanted", "1", "Acme", "Backend Engineer"),
        jd_markdown="# JD\n",
    )
    fallback = build_fallback_output(stored, stored.jd_markdown, "llm failed")

    assert validate_screening_structure(fallback) == (True, "")
    assert parse_match_table(fallback)[1] == "매칭 표 컬럼 부족"


def test_extract_tokens_drops_short_and_generic():
    tokens = extract_tokens("RDBMS(MariaDB) 기반 DB활용 경험 및 SQL 최적화")
    assert tokens == {"mariadb"}


def test_extract_tokens_returns_empty_for_korean_only_requirement():
    assert extract_tokens("대용량 트래픽 처리 경험") == set()


def _report(*rows: str, root: Path | None = None) -> tuple[EvidenceReport, list]:
    markdown = table(*rows)
    parsed, error = parse_match_table(markdown)
    assert error == ""
    report = check_rows(
        parsed,
        corpus=CORPUS,
        root=root or Path("/nonexistent-root"),
        declared=corpus_source_paths(CORPUS),
        basenames=corpus_basenames(CORPUS),
    )
    return report, parsed


def test_absent_technology_is_demoted():
    report, _ = _report("| MyBatis 사용 개발 | 필수 | 충족 | 있다고 주장 |")
    assert report.demoted_indices == (0,)
    assert report.unevidenced_keyword == 1
    assert report.unevidenced_keyword_strict == 1


def test_present_technology_is_untouched():
    report, _ = _report("| Spring Boot 기반 개발 | 필수 | 충족 | 다수 경험 |")
    assert report.demoted_indices == ()
    assert report.unevidenced_keyword == 0
    assert report.unevidenced_keyword_strict == 0


def test_or_list_with_one_present_alternative_is_not_demoted():
    report, _ = _report("| React/Vue 백오피스 개발 | 필수 | 충족 | Vue 경험 |")
    assert report.demoted_indices == ()
    assert report.unevidenced_keyword == 0
    assert report.unevidenced_keyword_strict == 1


def test_parenthesised_alternatives_are_not_demoted():
    report, _ = _report("| 클라우드 환경(AWS, GCP) 운영 | 필수 | 충족 | AWS 운영 |")
    assert report.demoted_indices == ()
    assert report.unevidenced_keyword_strict == 1


def test_generic_only_requirement_is_not_demoted():
    report, _ = _report("| SQL 작성, 쿼리 최적화 | 필수 | 충족 | 튜닝 경험 |")
    assert report.demoted_indices == ()
    assert report.unevidenced_keyword_strict == 0


# A Korean résumé carries none of the English nouns an English-language
# requirement is written from, so a row built only from connective words used to
# have every token absent and lost its 충족 claim. What each requirement is ABOUT
# — years, an API, a database — is not decided by those words.
@pytest.mark.parametrize(
    "requirement",
    [
        "Software Engineering 경력 6년 이상",
        "복잡한 Backend System을 직접 설계하고 Production 환경에서 운영",
        "SQL Database 이해와 실무 경험",
        "Legacy 코드 분석을 통한 리팩토링",
    ],
)
def test_connective_english_requirement_is_not_demoted(requirement: str):
    report, _ = _report(f"| {requirement} | 필수 | 충족 | 다수 경험 |")
    assert report.demoted_indices == ()
    assert report.unevidenced_keyword == 0


# The counterpart, and the test that fails if the generic set grows too far: when
# a requirement is ABOUT the absent word — a product, or a named practice the
# résumé never evidences — the claim must still lose. Listing such a token would
# empty the token set, skip the row, and let an unsupported 충족 reach 지원 추천.
@pytest.mark.parametrize(
    "requirement",
    [
        "PostgreSQL 기반 시스템 구축",
        "Oracle 마이그레이션 경험",
        "Elasticsearch 운영",
        "TDD를 적용해보셨거나 대규모 리팩토링을 주도해보신 분",
        "Machine Learning 제품 사용 등 관련 경험",
        "IoT·디바이스 연동 등 서버 밖의 기기와 통신해 본 경험",
        "JSON 통신 구조 이해",
        "B2G 서비스 구축 경험",
        "O2O 서비스 경험",
    ],
)
def test_absent_verifiable_claim_is_still_demoted(requirement: str):
    report, _ = _report(f"| {requirement} | 필수 | 충족 | 있다고 주장 |")
    assert report.demoted_indices == (0,)
    assert report.unevidenced_keyword == 1


def test_code_rendered_row_with_fabricated_resume_citation_is_demoted(tmp_path: Path):
    jd_markdown = """# Backend Role

## 자격 요건
- Spring Boot 개발
"""
    stored = StoredJobRecord(
        record=JobRecord("wanted", "1", "Acme", "Backend Engineer"),
        jd_markdown=jd_markdown,
    )
    manifest = extract_requirement_manifest(jd_markdown)
    assessment = parse_screening_assessment(
        """{
  "schema_version": 1,
  "matches": [
    {
      "id": "required-001",
      "match": "충족",
      "evidence": "[source: private/profile/fabricated.md] Spring Boot 경험"
    }
  ],
  "verdict": "지원 추천",
  "decision_basis": [],
  "screening_summary": ["구조화 평가 완료"],
  "reasons": [
    "후보자 이력의 명시 근거만 사용했다",
    "source-owned requirement manifest를 그대로 따랐다",
    "최종 판정은 구조화된 대조 결과로 작성했다"
  ]
}""",
        manifest,
    )
    markdown = render_screening_markdown(stored, jd_markdown, manifest, assessment)
    rows, error = parse_match_table(markdown)

    assert error == ""

    report = check_rows(
        rows,
        corpus=CORPUS,
        root=tmp_path,
        declared=corpus_source_paths(CORPUS),
        basenames=corpus_basenames(CORPUS),
    )

    assert report.missing_source_path == 1
    assert report.demoted_indices == (0,)


def test_category_term_with_present_product_is_not_demoted():
    report, _ = _report("| RDBMS(MariaDB) 활용 | 필수 | 충족 | MariaDB 구성 |")
    assert report.demoted_indices == ()


def test_non_missing_rows_are_never_keyword_checked():
    report, _ = _report("| MyBatis 사용 개발 | 필수 | 없음 | 확인되지 않음 |")
    assert report.demoted_indices == ()
    assert report.unevidenced_keyword_strict == 0


def test_fabricated_source_path_is_demoted(tmp_path):
    report, _ = _report(
        "| Spring Boot 개발 | 필수 | 충족 | 근거 "
        "[source: private/companies/F5/projects/3d-store-dashboard.md] |",
        root=tmp_path,
    )
    assert report.missing_source_path == 1
    assert report.demoted_indices == (0,)


def test_basename_resolvable_path_is_not_a_violation(tmp_path):
    report, _ = _report(
        "| Spring Boot 개발 | 필수 | 충족 | 근거 [source: examplecorp/projects/chatapp.md] |",
        root=tmp_path,
    )
    assert report.missing_source_path == 0
    assert report.demoted_indices == ()


def test_corpus_declared_path_is_not_a_violation(tmp_path):
    report, _ = _report(
        "| Spring Boot 개발 | 필수 | 충족 | 근거 [source: private/profile/skills-job.md] |",
        root=tmp_path,
    )
    assert report.missing_source_path == 0


def test_existing_non_corpus_file_is_a_violation(tmp_path):
    """A repository file existing on disk is not evidence: only paths the corpus
    declares through [source:] markers may be cited."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "getting-started.md").write_text("x", encoding="utf-8")
    report, _ = _report(
        "| Spring Boot 개발 | 필수 | 충족 | 근거 [source: docs/getting-started.md] |",
        root=tmp_path,
    )
    assert report.missing_source_path == 1
    assert report.demoted_indices == (0,)


@pytest.mark.parametrize("citation", ["프로젝트", "다수", "—", "전체 프로젝트 이력"])
def test_non_path_prose_citations_are_ignored(citation, tmp_path):
    report, _ = _report(
        f"| Spring Boot 개발 | 필수 | 충족 | 근거 [source: {citation}] |",
        root=tmp_path,
    )
    assert report.missing_source_path == 0
    assert report.demoted_indices == ()


def test_bare_markdown_filename_is_validated_against_the_allowlist(tmp_path):
    report, _ = _report(
        "| Spring Boot 개발 | 필수 | 충족 | 근거 [source: nonexistent.md] |",
        root=tmp_path,
    )
    assert report.missing_source_path == 1
    assert report.demoted_indices == (0,)


def test_bare_markdown_filename_in_the_corpus_resolves(tmp_path):
    report, _ = _report(
        "| Spring Boot 개발 | 필수 | 충족 | 근거 [source: skills-job.md] |",
        root=tmp_path,
    )
    assert report.missing_source_path == 0
    assert report.demoted_indices == ()


@pytest.mark.parametrize("suffix", [":12", ":L12", ":12-40", "#경력", "#L4-L9"])
def test_line_and_fragment_suffixes_do_not_hide_a_fabricated_path(suffix, tmp_path):
    report, _ = _report(
        f"| Spring Boot 개발 | 필수 | 충족 | 근거 "
        f"[source: private/companies/F5/projects/nope.md{suffix}] |",
        root=tmp_path,
    )
    assert report.missing_source_path == 1
    assert report.demoted_indices == (0,)


@pytest.mark.parametrize("suffix", [":12", "#경력"])
def test_line_and_fragment_suffixes_do_not_break_a_real_path(suffix, tmp_path):
    report, _ = _report(
        f"| Spring Boot 개발 | 필수 | 충족 | 근거 "
        f"[source: private/profile/skills-job.md{suffix}] |",
        root=tmp_path,
    )
    assert report.missing_source_path == 0


def test_corpus_basenames_reads_source_markers():
    assert corpus_basenames(CORPUS) == {"skills-job.md", "chatapp.md"}


AMBIGUOUS_CORPUS = """[source: private/companies/a/profile.md]
A사 프로필

[source: private/companies/b/profile.md]
B사 프로필

[source: private/profile/skills-job.md]
Spring Boot
"""


def test_a_basename_shared_by_multiple_sources_is_not_a_fallback():
    """Every company contributes its own profile.md, so the bare name
    identifies none of them — only unique names may back a bare citation."""
    assert corpus_basenames(AMBIGUOUS_CORPUS) == {"skills-job.md"}


def test_an_ambiguous_basename_citation_is_a_violation(tmp_path):
    unresolved = resolve_source_paths(
        "[source: profile.md] [source: invented/profile.md] "
        "[source: private/companies/a/profile.md]",
        root=tmp_path,
        declared=corpus_source_paths(AMBIGUOUS_CORPUS),
        basenames=corpus_basenames(AMBIGUOUS_CORPUS),
    )
    assert unresolved == ["profile.md", "invented/profile.md"]


def test_corpus_source_paths_reads_source_markers():
    assert corpus_source_paths(CORPUS) == {
        "private/profile/skills-job.md",
        "private/companies/examplecorp/projects/chatapp.md",
    }


def test_resolve_source_paths_returns_only_unresolved(tmp_path):
    text = (
        "[source: private/profile/skills-job.md] "
        "[source: private/companies/F5/projects/nope.md] "
        "[source: 다수]"
    )
    unresolved = resolve_source_paths(
        text,
        root=tmp_path,
        declared={"private/profile/skills-job.md"},
        basenames={"skills-job.md"},
    )
    assert unresolved == ["private/companies/F5/projects/nope.md"]


def test_apply_demotions_rewrites_only_targeted_rows():
    markdown = table(
        "| MyBatis 사용 개발 | 필수 | 충족 | 주장 |",
        "| Spring Boot 개발 | 필수 | 충족 | 근거 |",
    )
    rows, _ = parse_match_table(markdown)
    updated = apply_demotions(markdown, rows, (0,))
    assert "| MyBatis 사용 개발 | 필수 | 없음 | 주장 |" in updated
    assert "| Spring Boot 개발 | 필수 | 충족 | 근거 |" in updated
    reparsed, error = parse_match_table(updated)
    assert error == ""
    assert [r.match for r in reparsed] == ["없음", "충족"]


def test_apply_demotions_is_a_noop_without_targets():
    markdown = table("| Spring Boot 개발 | 필수 | 충족 | 근거 |")
    rows, _ = parse_match_table(markdown)
    assert apply_demotions(markdown, rows, ()) == markdown


@pytest.mark.parametrize(
    "citation",
    ["/etc/skills-job.md", "../../skills-job.md", "private/../../skills-job.md"],
)
def test_an_out_of_root_path_cannot_borrow_a_real_basename(citation, tmp_path):
    """The containment check has to run before the basename fallback, or a
    fabricated path reusing a real résumé file name resolves anyway."""
    unresolved = resolve_source_paths(
        f"[source: {citation}]",
        root=tmp_path,
        declared=set(),
        basenames={"skills-job.md"},
    )

    assert unresolved == [citation]


@pytest.mark.parametrize(
    "citation",
    ["/etc/hosts.md", "../../../outside/secret.md", "private/../../escaped.md"],
)
def test_paths_outside_the_workspace_are_never_treated_as_resolved(citation, tmp_path):
    outside = tmp_path.parent / "outside"
    outside.mkdir(exist_ok=True)
    (outside / "secret.md").write_text("x", encoding="utf-8")

    unresolved = resolve_source_paths(
        f"[source: {citation}]",
        root=tmp_path,
        declared=set(),
        basenames=set(),
    )

    assert unresolved == [citation]
