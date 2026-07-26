from __future__ import annotations

import re

import pytest

from careerkit.resume.application.verify_content import Claim, VerifierConfig, extract_claims, parse_resume_sections, verifier_config_from_data, verify_claims

TEST_ALIASES = {
    "AlphaCorp": ["알파코프", "AlphaCorp", "alphacorp"],
    "BetaLabs": ["베타랩", "BetaLabs", "betalabs"],
    "GammaTech": ["감마텍", "GammaTech", "gammatech"],
    "DeltaIO": ["델타아이오", "DeltaIO", "deltaio"],
}
TEST_PARENT_MAP = {"BetaLabs": "AlphaCorp", "GammaTech": "AlphaCorp"}
TEST_KEYWORDS = ["Redis", "JPA", "N+1", "fetch join", "커넥션 풀", "WebSocket", "GraphQL", "Docker", "Kafka"]
TEST_METRIC = re.compile(r"\d+%\s*(?:이상\s*|p\s*)?(?:단축|감소|개선|향상|절감|증가)")
TEST_CONFIG = VerifierConfig(TEST_ALIASES, TEST_PARENT_MAP, TEST_KEYWORDS, TEST_METRIC)


def _write(tmp_path, name: str, text: str):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_parse_resume_sections_and_verify(tmp_path) -> None:
    resume = _write(tmp_path, "resume.md", "# AlphaCorp (알파코프)\n\n커넥션 풀 안정화\n\n# BetaLabs (베타랩)\n\nN+1 문제 해결\n")
    sections = parse_resume_sections(resume, config=TEST_CONFIG)
    results = verify_claims([Claim(company_key="BetaLabs", keyword="N+1", line_number=1)], sections, config=TEST_CONFIG)
    assert "AlphaCorp" in sections and "BetaLabs" in sections
    assert results[0].status == "verified"


def test_extract_claims_and_parent_fallback(tmp_path) -> None:
    resume = _write(tmp_path, "resume.md", "# AlphaCorp (알파코프)\n\nRedis 캐시 도입\n")
    interview = _write(tmp_path, "interview.md", "> BetaLabs에서 Redis 캐시를 적용했습니다\n")
    claims = extract_claims(interview, config=TEST_CONFIG)
    redis_claims = [claim for claim in claims if claim.keyword == "Redis"]
    results = verify_claims(redis_claims, parse_resume_sections(resume, config=TEST_CONFIG), config=TEST_CONFIG)
    assert redis_claims
    assert all(result.status == "verified" for result in results)


def test_verify_ungrounded_claim(tmp_path) -> None:
    resume = _write(tmp_path, "resume.md", "# AlphaCorp\n\nRedis 캐시\n")
    interview = _write(tmp_path, "interview.md", "> BetaLabs에서 GraphQL을 사용했습니다\n")
    results = verify_claims(extract_claims(interview, config=TEST_CONFIG), parse_resume_sections(resume, config=TEST_CONFIG), config=TEST_CONFIG)
    assert any(result.status == "ungrounded" for result in results)


def test_loaded_verifier_detects_broad_unsupported_technology_claims(tmp_path) -> None:
    interview = _write(tmp_path, "interview.md", "> AlphaCorp에서 QueryDSL, Terraform, K8s, CQRS를 사용했습니다\n")
    config = verifier_config_from_data({
        "company_aliases": {"AlphaCorp": ["AlphaCorp"]},
        "parent_company_map": {},
        "technology_keywords": ["QueryDSL", "Terraform", "K8s"],
        "pattern_keywords": ["CQRS"],
    })

    keywords = {claim.keyword for claim in extract_claims(interview, config=config)}

    assert {"QueryDSL", "Terraform", "K8s", "CQRS"} <= keywords


def test_verifier_config_from_data_rejects_unknown_parent_company() -> None:
    with pytest.raises(ValueError, match="must reference declared"):
        verifier_config_from_data({
            "company_aliases": {"AlphaCorp": ["AlphaCorp"]},
            "parent_company_map": {"BetaLabs": "AlphaCorp"},
            "technology_keywords": ["Redis"],
            "pattern_keywords": [],
        })


def test_verifier_config_requires_generated_verification_keywords() -> None:
    with pytest.raises(ValueError, match="technology_keywords"):
        verifier_config_from_data({
            "company_aliases": {"AlphaCorp": ["AlphaCorp"]},
            "parent_company_map": {},
        })
