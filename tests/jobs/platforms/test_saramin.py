from __future__ import annotations

import textwrap
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from careerkit.jobs.adapters.platforms.saramin import (
    SaraminAdapter,
    SaraminCompanyInfo,
    extract_company_from_detail,
    extract_csn_from_html,
    extract_detail_fields,
    extract_jd_body,
    extract_jd_body_sections,
    extract_position_from_detail,
    format_company_markdown,
    parse_search_html,
)
from careerkit.jobs.application.company_info import parse_company_file, validate_company


SEARCH_CARD_HTML = textwrap.dedent("""\
    <svg xmlns="http://www.w3.org/2000/svg" style="display:none"></svg>
    <div id="list_54607844" class="recruit_container list_link recruit"
         data-data_layer="keyword_free|paid_n" data-rec_idx=54607844>
        <a href="/job-search/view?rec_idx=54607844" class="link">
            <div class="list">
                <p class="tit">킨코스코리아㈜ <strong>백엔드</strong> 개발자 모집</p>
                <div class="meta">
                    <span>서울 중구 외</span><span>직원대출제도</span>
                </div>
                <div class="corp">
                    <span class="corp_name">킨코스코리아(주)</span>
                </div>
                <div class="applicant"><span class="date">D-6</span></div>
            </div>
        </a>
    </div>
    <div id="list_53930400" class="recruit_container list_link recruit"
         data-data_layer="keyword_free|paid_n" data-rec_idx=53930400>
        <a href="/job-search/view?rec_idx=53930400" class="link">
            <div class="list">
                <p class="tit">[수산그룹] 백엔드 개발자 채용</p>
                <div class="meta">
                    <span>서울 강남구</span><span>신입·경력</span><span>대졸↑</span>
                </div>
                <div class="corp">
                    <span class="corp_name">(주)수산아이앤티</span>
                </div>
                <div class="applicant"><span class="date">~09.20(일)</span></div>
            </div>
        </a>
    </div>
    <div id="list_54700001" class="recruit_container list_link recruit"
         data-data_layer="keyword_free|paid_n" data-rec_idx=54700001>
        <a href="/job-search/view?rec_idx=54700001" class="link">
            <div class="list">
                <p class="tit">시니어 백엔드 개발자</p>
                <div class="meta">
                    <span>서울 성동구</span><span>경력3년↑</span><span>대졸↑</span>
                </div>
                <div class="corp">
                    <span class="corp_name">테스트컴퍼니</span>
                </div>
                <div class="applicant"><span class="date">D-14</span></div>
            </div>
        </a>
    </div>
""")

SEARCH_CARD_HTML_PAGE_2 = textwrap.dedent("""\
    <div id="list_54700002" class="recruit_container list_link recruit"
         data-data_layer="keyword_free|paid_n" data-rec_idx=54700002>
        <a href="/job-search/view?rec_idx=54700002" class="link">
            <div class="list">
                <p class="tit">플랫폼 백엔드 엔지니어</p>
                <div class="meta">
                    <span>서울 강남구</span><span>경력 5~10년</span><span>대졸↑</span>
                </div>
                <div class="corp">
                    <span class="corp_name">페이지투컴퍼니</span>
                </div>
                <div class="applicant"><span class="date">D-10</span></div>
            </div>
        </a>
    </div>
""")

DETAIL_PAGE_HTML = textwrap.dedent("""\
    <html>
    <head><title>[매드업] 백엔드 개발자 채용 (D-30) - 사람인</title></head>
    <body>
    <span class="corp_name">매드업</span>
    <a href="/job-search/company-info-view?csn=KzFkR3NMeit6Q05mekViUktDTUhRUT09">기업정보</a>
    <dl>
        <dt class="tit">근무형태</dt>
        <dd class="desc">정규직</dd>
        <dt class="tit">급여</dt>
        <dd class="desc">면접 후 결정</dd>
        <dt class="tit">지역</dt>
        <dd class="desc">서울 강남구</dd>
        <dt class="tit">경력</dt>
        <dd class="desc">경력 1~4년</dd>
        <dt class="tit">우대사항</dt>
        <dd class="desc">해당직무 근무경험</dd>
        <dt class="tit">전형절차</dt>
        <dd class="desc">서류전형 1차 인터뷰 2차 인터뷰 최종합격</dd>
        <dt class="tit">급여제도</dt>
        <dd class="desc">인센티브제,4대 보험</dd>
        <dt class="tit">조직문화</dt>
        <dd class="desc">수평적 조직문화,야근강요 안함</dd>
    </dl>
    <script>
        var detailContents_54616301 = {
            contents: 'PGI+66ek65Oc7JeFPC9iPjxicj7rsLHsl5Trk5wg6rCc67Cc7J6QIOyxhOyaqQ==',
            mobile_contents_yn: ''
        };
    </script>
    </body>
    </html>
""")

COMPANY_JSONLD_HTML = textwrap.dedent("""\
    <html>
    <head><title>(주)매드업 기업정보</title></head>
    <body>
    <script type="application/ld+json">
    {
        "@context": "http://schema.org",
        "@type": "Organization",
        "name": "(주)매드업 기업정보",
        "legalName": "(주)매드업",
        "foundingDate": "2015-01-29",
        "numberOfEmployees": {"@type": "QuantitativeValue", "value": "467"},
        "founder": {"@type": "Person", "name": "이동호/이주민"},
        "address": {"@type": "PostalAddress", "name": "서울 서초구 서초대로74길 4"},
        "makesOffer": {"@type": "Offer", "name": "광고 대행업"},
        "sameAs": ["http://madup.com/"],
        "description": "기업형태 : 중소기업, 스타트업, 업종 : 광고 대행업"
    }
    </script>
    </body>
</html>
""")

JD_BODY_TARGET_ALIASES = (
    "자격요건",
    "자격 요건",
    "지원자격",
    "지원 자격",
    "필수요건",
    "필수 요건",
    "우대사항",
    "우대 사항",
)

JD_BODY_BOUNDARY_LABELS = (
    "주요업무",
    "근무조건",
    "복리후생",
    "전형절차",
    "회사소개",
)


class TestParseSearchHtml:
    def test_extracts_cards_from_html(self) -> None:
        results = parse_search_html(SEARCH_CARD_HTML)
        assert len(results) == 3

    def test_first_card_fields(self) -> None:
        results = parse_search_html(SEARCH_CARD_HTML)
        card = results[0]
        assert card["id"] == "54607844"
        assert "백엔드" in card["title"]
        assert card["company"] == "킨코스코리아(주)"

    def test_second_card_has_experience(self) -> None:
        results = parse_search_html(SEARCH_CARD_HTML)
        card = results[1]
        assert card["id"] == "53930400"
        assert card["experience"] == "신입·경력"

    def test_first_card_no_experience(self) -> None:
        results = parse_search_html(SEARCH_CARD_HTML)
        assert results[0]["experience"] == ""

    def test_third_card_arrow_experience(self) -> None:
        results = parse_search_html(SEARCH_CARD_HTML)
        assert results[2]["experience"] == "경력3년↑"
        assert results[2]["company"] == "테스트컴퍼니"

    def test_empty_html_returns_empty(self) -> None:
        assert parse_search_html("") == []
        assert parse_search_html("<div>no cards</div>") == []


class TestDetailPageParsing:
    def test_extract_company(self) -> None:
        assert extract_company_from_detail(DETAIL_PAGE_HTML) == "매드업"

    def test_extract_position(self) -> None:
        position = extract_position_from_detail(DETAIL_PAGE_HTML)
        assert "백엔드 개발자 채용" in position
        assert "사람인" not in position

    def test_extract_csn(self) -> None:
        csn = extract_csn_from_html(DETAIL_PAGE_HTML)
        assert csn == "KzFkR3NMeit6Q05mekViUktDTUhRUT09"

    def test_extract_fields(self) -> None:
        fields = extract_detail_fields(DETAIL_PAGE_HTML)
        assert fields["근무형태"] == "정규직"
        assert fields["지역"] == "서울 강남구"
        assert fields["경력"] == "경력 1~4년"

    def test_extract_fields_keeps_scalar_values(self) -> None:
        html = textwrap.dedent("""\
            <dl>
                <dt class="tit">근무형태</dt>
                <dd class="desc">정규직</dd>
                <dt class="tit">급여</dt>
                <dd class="desc">면접 후 결정</dd>
            </dl>
        """)
        fields = extract_detail_fields(html)
        assert fields == {
            "근무형태": "정규직",
            "급여": "면접 후 결정",
        }

    def test_extract_fields_ignores_malformed_detail_block(self) -> None:
        html = textwrap.dedent("""\
            <dl>
                <dt class="tit">근무형태</dt>
                <dd class="desc">정규직
            </dl>
        """)
        assert extract_detail_fields(html) == {}

    def test_extract_fields_ignores_empty_detail_block(self) -> None:
        html = textwrap.dedent("""\
            <dl>
                <dt class="tit">우대사항</dt>
                <dd class="desc"></dd>
            </dl>
        """)
        assert extract_detail_fields(html) == {}

    def test_extract_fields_preserves_semantic_boundaries(self) -> None:
        html = textwrap.dedent("""\
            <dl>
                <dt class="tit">자격요건</dt>
                <dd class="desc">
                    <ul>
                        <li>Python 백엔드 개발 경험</li>
                        <li>SQL 활용 능력</li>
                    </ul>
                </dd>
                <dt class="tit">우대사항</dt>
                <dd class="desc">
                    <p>테스트 코드 작성 경험</p>
                    <p>Docker 운영 경험</p>
                </dd>
            </dl>
        """)
        fields = extract_detail_fields(html)
        assert fields["자격요건"] == "- Python 백엔드 개발 경험\n- SQL 활용 능력"
        assert fields["우대사항"] == "테스트 코드 작성 경험\nDocker 운영 경험"

    def test_extract_fields_does_not_invent_break_bullets(self) -> None:
        html = textwrap.dedent("""\
            <dl>
                <dt class="tit">근무환경</dt>
                <dd class="desc">원격 근무 가능<br>주 1회 오피스 출근</dd>
            </dl>
        """)
        fields = extract_detail_fields(html)
        assert fields["근무환경"] == "원격 근무 가능\n주 1회 오피스 출근"

    def test_extract_experience_from_meta_fallback(self) -> None:
        html_no_dt = textwrap.dedent("""\
            <html>
            <head>
            <meta name="description" content="매드업, 백엔드 개발자, 경력:경력 1~4년, 학력:대졸이상, 면접 후 결정" >
            </head>
            <body>
            <dl>
                <dt class="tit">지역</dt>
                <dd class="desc">서울 강남구</dd>
            </dl>
            </body></html>
        """)
        fields = extract_detail_fields(html_no_dt)
        assert fields["경력"] == "경력 1~4년"
        assert fields["지역"] == "서울 강남구"

    def test_extract_experience_dt_takes_priority(self) -> None:
        fields = extract_detail_fields(DETAIL_PAGE_HTML)
        assert fields["경력"] == "경력 1~4년"

    def test_extract_jd_body(self) -> None:
        body = extract_jd_body(DETAIL_PAGE_HTML, "54616301")
        assert len(body) > 0

    def test_extract_jd_body_missing_returns_empty(self) -> None:
        assert extract_jd_body("<html></html>", "99999") == ""

    def test_extract_jd_body_preserves_semantic_boundaries(self) -> None:
        html = textwrap.dedent("""\
            <script>
                var detailContents_100 = {
                    contents: 'PHA+4pagIOyekOqyqeyalOqxtDwvcD48dWw+PGxpPlB5dGhvbiDrsLHsl5Trk5wg6rCc67CcIOqyve2XmDwvbGk+PGxpPlNRTCDtmZzsmqkg64ql66ClPC9saT48L3VsPjxwPigxKSDsmrDrjIAg7IKs7ZWtPC9wPjxwPu2FjOyKpO2KuCDsvZTrk5wg7J6R7ISx6rK97ZeYPC9wPg==',
                    mobile_contents_yn: ''
                };
            </script>
        """)
        body = extract_jd_body(html, "100")
        assert body == "■ 자격요건\n- Python 백엔드 개발 경험\n- SQL 활용 능력\n(1) 우대 사항\n테스트 코드 작성경험"

    @pytest.mark.parametrize("heading", JD_BODY_TARGET_ALIASES)
    def test_extract_jd_body_sections_supports_all_target_aliases(self, heading: str) -> None:
        body = textwrap.dedent(f"""\
            {heading}
            Python 백엔드 개발 경험
            * 테스트 코드 작성 경험
        """)
        key = "우대사항" if "우대" in heading else "자격요건"
        assert extract_jd_body_sections(body) == {
            key: "- Python 백엔드 개발 경험\n- 테스트 코드 작성 경험",
        }

    @pytest.mark.parametrize("boundary_label", JD_BODY_BOUNDARY_LABELS)
    def test_extract_jd_body_sections_stops_at_each_boundary_group(self, boundary_label: str) -> None:
        body = textwrap.dedent(f"""\
            ■ 자격요건
            Python 백엔드 개발 경험
            {boundary_label}
            원격 근무 가능
            (1) 우대 사항
            • 테스트 코드 작성 경험
            + Docker 운영 경험
        """)
        assert extract_jd_body_sections(body) == {
            "자격요건": "- Python 백엔드 개발 경험",
            "우대사항": "- 테스트 코드 작성 경험\n- Docker 운영 경험",
        }

    @pytest.mark.parametrize(
        "body",
        (
            "",
            "채용 상세가 없습니다",
            "주요업무\nPython 백엔드 개발",
        ),
    )
    def test_extract_jd_body_sections_returns_empty_without_target(self, body: str) -> None:
        assert extract_jd_body_sections(body) == {}


class TestSaraminAdapter:
    def test_supports_search(self) -> None:
        adapter = SaraminAdapter()
        assert adapter.supports_search is True
        assert adapter.name == "saramin"

    def test_search_returns_candidates(self) -> None:
        http = StubSearchHttp(SEARCH_CARD_HTML, count="3")
        config = SimpleNamespace(
            http_client=http,
            platforms={"saramin": SimpleNamespace(base_url="https://www.saramin.co.kr")},
            rate_limits={},
            filters={},
        )
        result = SaraminAdapter().search("백엔드", config=config, state=None, http=http)
        assert len(result.items) == 3
        assert result.items[0].platform == "saramin"
        assert result.items[0].job_id == "54607844"
        assert "백엔드" in result.items[0].title
        assert result.items[0].company == "킨코스코리아(주)"

    def test_search_url_format(self) -> None:
        http = StubSearchHttp(SEARCH_CARD_HTML, count="3")
        config = SimpleNamespace(
            http_client=http,
            platforms={"saramin": SimpleNamespace(base_url="https://www.saramin.co.kr")},
            rate_limits={},
            filters={},
        )
        result = SaraminAdapter().search("백엔드", config=config, state=None, http=http)
        assert result.items[0].url == "https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=54607844"

    def test_search_empty_response(self) -> None:
        http = StubSearchHttp("", count="0")
        config = SimpleNamespace(
            http_client=http,
            platforms={"saramin": SimpleNamespace(base_url="https://www.saramin.co.kr")},
            rate_limits={},
            filters={},
        )
        result = SaraminAdapter().search("없는키워드", config=config, state=None, http=http)
        assert len(result.items) == 0

    def test_not_query_independent(self) -> None:
        assert getattr(SaraminAdapter(), "query_independent", False) is False

    def test_search_omits_native_experience_params_without_minimum(self) -> None:
        http = StubSearchHttp([{"count": "0", "innerHTML": ""}])
        SaraminAdapter().search(
            "백엔드",
            config=_config(http, api_min_experience=None, api_max_experience=10),
            state=None,
            http=http,
        )
        query = parse_qs(urlparse(http.urls[0]).query)
        assert "exp_cd" not in query
        assert "exp_min" not in query
        assert "exp_max" not in query
        assert "exp_none" not in query

    def test_search_omits_native_experience_params_without_maximum(self) -> None:
        http = StubSearchHttp([{"count": "0", "innerHTML": ""}])
        SaraminAdapter().search(
            "백엔드",
            config=_config(http, api_min_experience=5, api_max_experience=None),
            state=None,
            http=http,
        )
        query = parse_qs(urlparse(http.urls[0]).query)
        assert "exp_cd" not in query
        assert "exp_min" not in query
        assert "exp_max" not in query
        assert "exp_none" not in query

    def test_search_serializes_configured_experience_range_on_first_and_next_pages(self) -> None:
        http = StubSearchHttp([
            {"count": "4", "innerHTML": SEARCH_CARD_HTML},
            {"count": "4", "innerHTML": SEARCH_CARD_HTML_PAGE_2},
        ])
        result = SaraminAdapter().search(
            "백엔드",
            config=_config(http, api_min_experience=5, api_max_experience=10),
            state=None,
            http=http,
        )
        assert [item.job_id for item in result.items] == ["54607844", "53930400", "54700001", "54700002"]
        first_query = parse_qs(urlparse(http.urls[0]).query)
        second_query = parse_qs(urlparse(http.urls[1]).query)
        assert first_query["page"] == ["1"]
        assert second_query["page"] == ["2"]
        assert first_query["exp_cd"] == ["2"]
        assert first_query["exp_min"] == ["5"]
        assert first_query["exp_max"] == ["10"]
        assert first_query["exp_none"] == ["y"]
        assert second_query["exp_cd"] == ["2"]
        assert second_query["exp_min"] == ["5"]
        assert second_query["exp_max"] == ["10"]
        assert second_query["exp_none"] == ["y"]

    def test_search_serializes_browser_captured_experience_contract(self) -> None:
        http = StubSearchHttp([{"count": "0", "innerHTML": ""}])
        SaraminAdapter().search(
            "백엔드",
            config=_config(http, api_min_experience=5, api_max_experience=10),
            state=None,
            http=http,
        )
        assert "exp_cd=2&exp_min=5&exp_max=10&exp_none=y" in http.urls[0]

    def test_search_returns_partial_items_after_second_page_failure_and_keeps_career_params(self) -> None:
        http = StubSearchHttp([
            {"count": "4", "innerHTML": SEARCH_CARD_HTML},
            RuntimeError("page 2 unavailable"),
        ])
        result = SaraminAdapter().search(
            "백엔드",
            config=_config(http, api_min_experience=5, api_max_experience=10),
            state=None,
            http=http,
        )
        assert [item.job_id for item in result.items] == ["54607844", "53930400", "54700001"]
        assert result.complete is False
        assert result.pages_fetched == 1
        first_query = parse_qs(urlparse(http.urls[0]).query)
        second_query = parse_qs(urlparse(http.urls[1]).query)
        assert first_query["exp_cd"] == ["2"]
        assert first_query["exp_min"] == ["5"]
        assert first_query["exp_max"] == ["10"]
        assert first_query["exp_none"] == ["y"]
        assert second_query["exp_cd"] == ["2"]
        assert second_query["exp_min"] == ["5"]
        assert second_query["exp_max"] == ["10"]
        assert second_query["exp_none"] == ["y"]


class TestCompanyInfo:
    def test_parse_jsonld(self) -> None:
        from careerkit.jobs.adapters.platforms.saramin import _parse_company_jsonld

        info = _parse_company_jsonld(COMPANY_JSONLD_HTML)
        assert info.name == "(주)매드업"
        assert info.founded_date == "2015-01-29"
        assert info.employee_count == 467
        assert info.ceo == "이동호/이주민"
        assert info.industry == "광고 대행업"
        assert info.homepage == "http://madup.com/"

    def test_company_type_from_description(self) -> None:
        from careerkit.jobs.adapters.platforms.saramin import _parse_company_jsonld

        info = _parse_company_jsonld(COMPANY_JSONLD_HTML)
        assert "중소기업" in info.company_type

    def test_format_company_markdown_roundtrip(self, tmp_path: Path) -> None:
        info = SaraminCompanyInfo(
            name="테스트회사",
            industry="IT서비스",
            company_type="중소기업",
            founded_date="2020-03-15",
            employee_count=150,
            avg_salary_manwon=5000,
            ceo="홍길동",
            address="서울 강남구",
            homepage="https://test.com",
        )
        md = format_company_markdown(info)
        path = tmp_path / "테스트회사.md"
        path.write_text(md, encoding="utf-8")

        parsed = parse_company_file(path)
        assert parsed.name == "테스트회사"
        assert parsed.founded_year == 2020
        assert parsed.employee_current == 150
        assert parsed.avg_salary == 5000
        assert parsed.industry == "IT서비스"

    def test_roundtrip_completeness_above_70(self, tmp_path: Path) -> None:
        info = SaraminCompanyInfo(
            name="완결회사",
            industry="제조업",
            company_type="중견기업",
            founded_date="2010-06-01",
            employee_count=500,
            avg_salary_manwon=6000,
            ceo="김사장",
            address="서울 송파구",
            homepage="https://example.com",
        )
        md = format_company_markdown(info)
        path = tmp_path / "완결회사.md"
        path.write_text(md, encoding="utf-8")

        parsed = parse_company_file(path)
        result = validate_company(parsed, path)
        assert result.completeness_score >= 70

    def test_startup_type_not_emitted(self, tmp_path: Path) -> None:
        info = SaraminCompanyInfo(
            name="벤처사",
            industry="IT",
            company_type="스타트업",
            founded_date="2022-01-01",
            employee_count=30,
            ceo="대표",
            address="서울",
            homepage="",
        )
        md = format_company_markdown(info)
        assert "스타트업 여부 | 아니오" in md

        path = tmp_path / "벤처사.md"
        path.write_text(md, encoding="utf-8")
        parsed = parse_company_file(path)
        assert parsed.is_startup is False

    def test_startup_type_completeness_safe(self, tmp_path: Path) -> None:
        info = SaraminCompanyInfo(
            name="스타트업사",
            industry="광고",
            company_type="스타트업",
            founded_date="2019-05-01",
            employee_count=50,
            ceo="CEO",
            address="서울",
            homepage="",
        )
        md = format_company_markdown(info)
        path = tmp_path / "스타트업사.md"
        path.write_text(md, encoding="utf-8")

        parsed = parse_company_file(path)
        result = validate_company(parsed, path)
        assert result.completeness_score >= 70


class StubSearchHttp:
    def __init__(self, responses: str | list[dict | Exception], count: str = "0") -> None:
        if isinstance(responses, list):
            self.responses = list(responses)
        else:
            self.responses = [{"count": count, "innerHTML": responses}]
        self.urls: list[str] = []

    def request_json(self, url: str, **kwargs) -> dict:
        self.urls.append(url)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def request_text(self, url: str, **kwargs) -> str:
        return ""


def _config(
    http: StubSearchHttp,
    *,
    api_min_experience: int | None = None,
    api_max_experience: int | None = None,
):
    return SimpleNamespace(
        http_client=http,
        platforms={"saramin": SimpleNamespace(base_url="https://www.saramin.co.kr")},
        rate_limits={},
        filters={
            "api_min_experience": api_min_experience,
            "api_max_experience": api_max_experience,
        },
    )
