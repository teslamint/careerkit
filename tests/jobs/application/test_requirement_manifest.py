from __future__ import annotations

import pytest

from careerkit.jobs.application.requirement_manifest import (
    RequirementKind,
    aggregate_parent_matches,
    extract_requirement_manifest,
    without_main_duty,
)


def test_extract_requirement_manifest_maps_sections_to_fixed_kinds() -> None:
    manifest = extract_requirement_manifest(
        """
## 자격 요건
- Python 백엔드 개발 경험
- 필수: 대용량 트래픽 처리 경험

## 주요 업무
- 결제 서비스 운영

## 우대 사항
- Kafka 사용 경험
""".strip()
    )

    assert [item.text for item in manifest.parents] == [
        "Python 백엔드 개발 경험",
        "필수: 대용량 트래픽 처리 경험",
        "결제 서비스 운영",
        "Kafka 사용 경험",
    ]
    assert [item.kind for item in manifest.parents] == [
        RequirementKind.REQUIRED,
        RequirementKind.REQUIRED,
        RequirementKind.MAIN_DUTY,
        RequirementKind.PREFERRED,
    ]
    assert manifest.parents[1].decisive is True
    assert manifest.parents[2].kind is RequirementKind.MAIN_DUTY
    assert manifest.ambiguous_qualifications is False


def test_extract_requirement_manifest_assigns_stable_distinct_ids_and_skips_empty_sections() -> None:
    manifest = extract_requirement_manifest(
        """
## 자격요건
- Python
- Python

## 우대사항

## 주요업무
- Python
""".strip()
    )

    assert [item.id for item in manifest.parents] == [
        "required-001",
        "required-002",
        "main_duty-001",
    ]
    assert len({item.id for item in manifest.parents}) == 3
    assert [item.text for item in manifest.parents] == ["Python", "Python", "Python"]


def test_extract_requirement_manifest_splits_composite_requirements_and_nested_bullets() -> None:
    jd_markdown = """
## 자격 요건
- Python, FastAPI / Django · PostgreSQL; AWS
  - Redis
  - Docker
""".strip()
    manifest = extract_requirement_manifest(jd_markdown)

    parent = manifest.parents[0]
    children = [item for item in manifest.items if item.parent_id == parent.id]

    assert parent.assessable is False
    assert [item.text for item in children] == [
        "Python",
        "FastAPI",
        "Django",
        "PostgreSQL",
        "AWS",
        "Redis",
        "Docker",
    ]
    assert [item.id for item in children] == [
        "required-001.1",
        "required-001.2",
        "required-001.3",
        "required-001.4",
        "required-001.5",
        "required-001.6",
        "required-001.7",
    ]
    assert len({item.id for item in children}) == len(children)
    for child in children:
        assert child.source_span is not None
        start, end = child.source_span
        assert jd_markdown[start:end] == child.text
    assert aggregate_parent_matches(
        manifest,
        {
            children[0].id: "충족",
            children[1].id: "충족",
            children[2].id: "부분",
            children[3].id: "없음",
            children[4].id: "충족",
            children[5].id: "부분",
            children[6].id: "충족",
        },
    ) == {parent.id: "부분"}


def test_extract_requirement_manifest_marks_prose_only_qualifications_as_ambiguous() -> None:
    manifest = extract_requirement_manifest(
        """
## 자격 요건
고객 중심으로 협업하고 빠르게 배우는 분을 찾습니다.
팀과 열린 커뮤니케이션이 중요합니다.
""".strip()
    )

    assert manifest.parents == ()
    assert manifest.leaves == ()
    assert manifest.ambiguous_qualifications is True


def test_extract_requirement_manifest_parses_unicode_bullet_and_ignores_unknown_headings() -> None:
    manifest = extract_requirement_manifest(
        """
## 포지션 소개
• 소개 bullet

## 주요 업무
• 레거시 bullet 업무

## 자격 요건
• 레거시 bullet 자격
""".strip()
    )

    assert len(manifest.parents) == 2
    assert manifest.parents[0].text == "레거시 bullet 업무"
    assert manifest.parents[0].kind == RequirementKind.MAIN_DUTY
    assert manifest.parents[1].text == "레거시 bullet 자격"
    assert manifest.parents[1].kind == RequirementKind.REQUIRED
    assert manifest.ambiguous_qualifications is False


def test_extract_requirement_manifest_ignores_unknown_headings_and_administrative_rows() -> None:
    manifest = extract_requirement_manifest(
        """
## 소개
- 우리 팀 소개

## 자격 요건
- 서류 제출 가능하신 분
- 경력증명서 제출 가능자

## 참고
- 기타 안내
""".strip()
    )

    assert manifest.parents == ()
    assert manifest.ambiguous_qualifications is False


def test_extract_requirement_manifest_keeps_plain_required_lines_ambiguous_even_with_decisive_markers() -> None:
    manifest = extract_requirement_manifest(
        """
## 자격 요건
필수: 빠르게 배우고 협업할 수 있는 분
must communicate clearly with the team

## 주요 업무
- API 개발
""".strip()
    )

    assert [item.text for item in manifest.parents] == ["API 개발"]
    assert manifest.parents[0].kind is RequirementKind.MAIN_DUTY
    assert manifest.ambiguous_qualifications is True


def test_extract_requirement_manifest_ignores_plain_and_bullet_information_missing() -> None:
    manifest = extract_requirement_manifest(
        """
## 자격 요건
정보 없음
- 정보 없음

## 주요 업무
- 정보 없음

## 우대사항
- 정보 없음
""".strip()
    )

    assert manifest.parents == ()
    assert manifest.leaves == ()
    assert manifest.ambiguous_qualifications is False


@pytest.mark.parametrize(
    ("leaf_matches", "expected"),
    [
        ({"required-001.1": "충족", "required-001.2": "충족"}, "충족"),
        ({"required-001.1": "없음", "required-001.2": "없음"}, "없음"),
        ({"required-001.1": "충족", "required-001.2": "없음"}, "부분"),
    ],
)
def test_aggregate_parent_matches_uses_parent_rows_only(
    leaf_matches: dict[str, str],
    expected: str,
) -> None:
    manifest = extract_requirement_manifest(
        """
## 자격 요건
- Python / Django
""".strip()
    )

    assert aggregate_parent_matches(manifest, leaf_matches) == {"required-001": expected}


def test_extract_requirement_manifest_preserves_atomic_slashes() -> None:
    jd_markdown = """
## 자격 요건
- CI/CD, FastAPI / Django
""".strip()

    manifest = extract_requirement_manifest(jd_markdown)

    parent = manifest.parents[0]
    children = [item for item in manifest.items if item.parent_id == parent.id]

    assert [item.text for item in children] == ["CI/CD", "FastAPI", "Django"]
    for child in children:
        assert child.source_span is not None
        start, end = child.source_span
        assert jd_markdown[start:end] == child.text


def test_extract_requirement_manifest_splits_bare_slash_composites() -> None:
    jd_markdown = """
## 자격 요건
- Python/Java, AWS/GCP
""".strip()

    manifest = extract_requirement_manifest(jd_markdown)

    parent = manifest.parents[0]
    children = [item for item in manifest.items if item.parent_id == parent.id]

    assert [item.text for item in children] == ["Python", "Java", "AWS", "GCP"]


def test_without_main_duty_removes_main_duty_keeps_required_and_preferred() -> None:
    manifest = extract_requirement_manifest(
        """
## 자격 요건
- Python 개발 경험

## 주요 업무
- 결제 서비스 운영

## 우대 사항
- Kafka 경험
""".strip()
    )

    filtered = without_main_duty(manifest)

    assert len(filtered.parents) == 2
    assert all(p.kind != RequirementKind.MAIN_DUTY for p in filtered.parents)
    assert filtered.parents[0].text == "Python 개발 경험"
    assert filtered.parents[1].text == "Kafka 경험"
    assert all(item.kind != RequirementKind.MAIN_DUTY for item in filtered.items)
    assert all(leaf.kind != RequirementKind.MAIN_DUTY for leaf in filtered.leaves)


def test_without_main_duty_preserves_ambiguous_qualifications() -> None:
    manifest = extract_requirement_manifest(
        """
## 자격 요건
빠르게 배우는 분을 찾습니다.
""".strip()
    )

    assert manifest.ambiguous_qualifications is True
    filtered = without_main_duty(manifest)
    assert filtered.ambiguous_qualifications is True


def test_without_main_duty_on_main_duty_only_returns_empty() -> None:
    manifest = extract_requirement_manifest(
        """
## 주요 업무
- 결제 서비스 운영
- 배포 파이프라인 관리
""".strip()
    )

    filtered = without_main_duty(manifest)

    assert filtered.parents == ()
    assert filtered.leaves == ()
    assert filtered.items == ()


def test_bracket_boundary_unknown_resets_kind_and_excludes_items() -> None:
    manifest = extract_requirement_manifest(
        """
## 자격 요건
- Python 개발 경험 3년 이상

[이런 분과 함께하고 싶어요]
- 주체적으로 문제를 해결하는 분
- 유연한 커뮤니케이션이 가능한 분
""".strip()
    )

    assert len(manifest.parents) == 1
    assert manifest.parents[0].text == "Python 개발 경험 3년 이상"
    assert manifest.parents[0].kind == RequirementKind.REQUIRED
    assert manifest.ambiguous_qualifications is True


def test_bracket_boundary_known_maps_to_section_kind() -> None:
    manifest = extract_requirement_manifest(
        """
[자격 요건]
- Python 개발 경험

[우대사항]
- Kafka 경험
""".strip()
    )

    assert len(manifest.parents) == 2
    assert manifest.parents[0].kind == RequirementKind.REQUIRED
    assert manifest.parents[0].text == "Python 개발 경험"
    assert manifest.parents[1].kind == RequirementKind.PREFERRED
    assert manifest.parents[1].text == "Kafka 경험"


def test_bracket_heading_echo_preserves_kind() -> None:
    manifest = extract_requirement_manifest(
        """
## 자격 요건
- Python 개발 경험

[자격 요건]
- Django 경험
""".strip()
    )

    assert len(manifest.parents) == 2
    assert all(p.kind == RequirementKind.REQUIRED for p in manifest.parents)
    assert manifest.ambiguous_qualifications is False


def test_bracket_boundary_sets_ambiguous_when_leaving_required() -> None:
    manifest = extract_requirement_manifest(
        """
## 자격 요건

[팀 소개]
- 우리 팀은 이렇습니다
""".strip()
    )

    assert manifest.parents == ()
    assert manifest.ambiguous_qualifications is True


def test_bracket_boundary_no_ambiguous_when_leaving_non_required() -> None:
    manifest = extract_requirement_manifest(
        """
## 우대사항
- Kafka 경험

[팀 소개]
- 우리 팀은 이렇습니다
""".strip()
    )

    assert len(manifest.parents) == 1
    assert manifest.parents[0].text == "Kafka 경험"
    assert manifest.ambiguous_qualifications is False


def test_indented_bracket_line_not_treated_as_boundary() -> None:
    manifest = extract_requirement_manifest(
        """
## 자격 요건
- Python 개발 경험
  [세부 사항]
- Django 경험
""".strip()
    )

    assert len(manifest.parents) == 2
    assert manifest.parents[0].text == "Python 개발 경험"
    assert manifest.parents[1].text == "Django 경험"


def test_bracket_in_markdown_link_not_treated_as_boundary() -> None:
    manifest = extract_requirement_manifest(
        """
## 자격 요건
- Python 개발 경험
[회사 소개](https://example.com)
- Django 경험
""".strip()
    )

    assert len(manifest.parents) == 2
    assert manifest.parents[0].text == "Python 개발 경험"
    assert manifest.parents[1].text == "Django 경험"


def test_bracket_boundary_resets_parent_index() -> None:
    manifest = extract_requirement_manifest(
        """
## 자격 요건
- Python, Django

[이런 분을 찾아요]
- 열정적인 분

## 우대사항
- Kafka 경험
""".strip()
    )

    parent = manifest.parents[0]
    children = [i for i in manifest.items if i.parent_id == parent.id]
    assert len(children) == 2
    assert children[0].text == "Python"
    assert children[1].text == "Django"
    assert len(manifest.parents) == 2
    assert manifest.parents[1].text == "Kafka 경험"
    assert manifest.parents[1].kind == RequirementKind.PREFERRED


def test_mixed_heading_and_bracket_sections() -> None:
    manifest = extract_requirement_manifest(
        """
## 주요 업무
- API 개발

[자격 요건]
- 3년 이상 경력

## 우대사항
- AWS 경험
""".strip()
    )

    assert len(manifest.parents) == 3
    assert manifest.parents[0].kind == RequirementKind.MAIN_DUTY
    assert manifest.parents[1].kind == RequirementKind.REQUIRED
    assert manifest.parents[2].kind == RequirementKind.PREFERRED


def test_new_section_kinds_basic_qualifications() -> None:
    manifest = extract_requirement_manifest(
        """
## Basic Qualifications
- 3 years backend experience
""".strip()
    )

    assert len(manifest.parents) == 1
    assert manifest.parents[0].kind == RequirementKind.REQUIRED


def test_new_section_kinds_key_responsibilities() -> None:
    manifest = extract_requirement_manifest(
        """
## Key Responsibilities
- Build APIs
""".strip()
    )

    assert len(manifest.parents) == 1
    assert manifest.parents[0].kind == RequirementKind.MAIN_DUTY


def test_extract_requirement_manifest_parses_unicode_bullet() -> None:
    manifest = extract_requirement_manifest(
        """
## 자격 요건
• 백엔드 개발 7년 이상 경력
• RDBMS 설계 및 성능 최적화
""".strip()
    )

    assert len(manifest.parents) == 2
    assert manifest.parents[0].text == "백엔드 개발 7년 이상 경력"
    assert manifest.parents[1].text == "RDBMS 설계 및 성능 최적화"
    assert manifest.ambiguous_qualifications is False
