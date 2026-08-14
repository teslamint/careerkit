from careerkit.jobs.application.title_filter import requirements_show_backend


BACKEND_JD = """# 합성 공고

## 자격요건

- Python 기반 API 서버 개발 경험 3년 이상
- 관계형 데이터베이스 스키마 설계 경험
"""

NON_BACKEND_JD = """# 합성 공고

## 자격요건

- React 기반 웹 UI 개발 경험 3년 이상
- 디자인 시스템 컴포넌트 구현 경험
"""

FRONTEND_CALLING_A_SERVER_JD = """# 합성 공고

## 자격요건

- REST API 서버와 통신하는 웹 프론트엔드 개발 경험 3년 이상
- 브라우저 렌더링 성능 최적화 경험
"""

PREFERRED_ONLY_BACKEND_JD = """# 합성 공고

## 자격요건

- 대규모 트래픽을 다뤄 본 경험
- 동료와 함께 설계를 검토해 본 경험

## 우대사항

- 인증 데이터를 처리하는 백엔드 플랫폼 운영 경험
"""

NO_REQUIREMENT_SECTION_JD = """# 합성 공고

## 회사 소개

- 합성 회사는 합성 제품을 만듭니다
- API 서버 운영 조직이 함께 성장합니다
"""


def test_requirements_show_backend_true_for_api_server_requirement() -> None:
    assert requirements_show_backend(BACKEND_JD) is True


def test_requirements_show_backend_false_for_empty_manifest() -> None:
    assert requirements_show_backend(NO_REQUIREMENT_SECTION_JD) is False


def test_requirements_show_backend_false_for_non_backend_requirements() -> None:
    assert requirements_show_backend(NON_BACKEND_JD) is False


def test_requirements_show_backend_accepts_a_server_mention_in_a_frontend_requirement() -> None:
    # Accepted trade-off, pinned so a later change has to argue with it: the check is a
    # token match, so a frontend requirement that merely names a server confirms. The
    # cost is one screening call, which the rules file then judges; the opposite error
    # sets a real backend role aside with no document behind it.
    assert requirements_show_backend(FRONTEND_CALLING_A_SERVER_JD) is True


def test_requirements_show_backend_accepts_a_preferred_only_backend_mention() -> None:
    # `parents` spans 자격요건, 주요업무, and 우대사항 — deliberately wider than the
    # screening path, which strips 주요업무. Measured on the corpus: this is what
    # confirms a Backend Engineer posting whose 자격요건 never says 백엔드.
    assert requirements_show_backend(PREFERRED_ONLY_BACKEND_JD) is True
