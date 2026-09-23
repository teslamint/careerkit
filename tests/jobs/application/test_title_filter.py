import pytest

from careerkit.jobs.application.title_filter import duties_show_server_work, requirements_show_backend


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

NESTED_BACKEND_JD = """# 합성 공고

## 자격요건

- 아래 항목 중 하나 이상에 해당하는 분
  - 합성 프레임워크 기반 백엔드 서비스 운영 경험
  - 합성 대시보드 화면 구현 경험
- 동료 리뷰 참여 경험
"""

NO_REQUIREMENT_SECTION_JD = """# 합성 공고

## 회사 소개

- 합성 회사는 합성 제품을 만듭니다
- API 서버 운영 조직이 함께 성장합니다
"""


def test_requirements_show_backend_true_for_api_server_requirement() -> None:
    assert requirements_show_backend(BACKEND_JD) is True


def test_requirements_show_backend_true_for_a_nested_requirement() -> None:
    # The outer bullet is generic and the backend evidence is indented under it.
    # Scanning parents alone returns False and sets a real backend posting aside.
    assert requirements_show_backend(NESTED_BACKEND_JD) is True


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


def _duty_jd(duty: str) -> str:
    return f"# 합성 공고\n\n## 주요업무\n\n- {duty}\n\n## 자격요건\n\n- 동료 리뷰 참여 경험\n"


# Authored for this rule, not copied from the corpus it was tuned on: these are the
# confirmation set. The API has to be what the role builds or runs.
@pytest.mark.parametrize(
    "duty",
    [
        "정산 API 설계 및 운영",
        "Kotlin 기반 주문 API와 배치 작업 개발",
        "모바일 앱에서 호출하는 인증 API 구현",
        "Spring 기반 예약 API 고도화",
        "Design, build and maintain RESTful APIs for the booking product",
        "OpenAPI 명세 기반 검색 API 개발",
    ],
)
def test_duties_show_server_work_for_api_the_role_builds(duty: str) -> None:
    assert duties_show_server_work(_duty_jd(duty)) is True


@pytest.mark.parametrize(
    "duty",
    [
        # SDK or library surface: the same words, but no server behind them.
        "개발자가 쓰기 쉬운 API와 SDK를 설계",
        "사내 공용 라이브러리의 API 설계 및 문서화",
        # Consuming someone else's API.
        "외부 결제사 API 연동 및 정산 화면 구현",
        "지도 API를 활용한 경로 탐색 기능 개발",
        "Build internal tools on our public API",
        "오픈 API 기반 데이터 수집기 개발",
        # The positive shape matches here; only the consumption guards reject it.
        "공공 오픈 API/웹 페이지 수집 크롤러 개발",
        "관제 API, 차량 단말 연동 및 운영",
        # Low-level work that exposes an API without being a service.
        "센서 드라이버 API 설계 및 구현",
        "임베디드 보드용 제어 API 개발",
        # No API or server in the duty at all.
        "React 기반 관리자 화면 개발",
    ],
)
def test_duties_show_server_work_rejects_other_api_contexts(duty: str) -> None:
    assert duties_show_server_work(_duty_jd(duty)) is False


def test_duties_show_server_work_ignores_api_work_outside_main_duties() -> None:
    # Only 주요업무 describes the role; a requirement line is covered by
    # requirements_show_backend and must not be reinterpreted here.
    jd = "# 합성 공고\n\n## 주요업무\n\n- 관리자 화면 개발\n\n## 자격요건\n\n- 정산 API 설계 및 운영 경험\n"
    assert duties_show_server_work(jd) is False
