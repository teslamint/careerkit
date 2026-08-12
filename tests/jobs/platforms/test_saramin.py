from __future__ import annotations

import base64
from collections.abc import Sequence
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
    extract_detail_sections,
    extract_jd_body,
    extract_jd_sections,
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


def _search_card_html(job_id: int) -> str:
    return textwrap.dedent(
        f"""\
        <div id="list_{job_id}" class="recruit_container list_link recruit"
             data-data_layer="keyword_free|paid_n" data-rec_idx={job_id}>
            <a href="/job-search/view?rec_idx={job_id}" class="link">
                <div class="list">
                    <p class="tit">Backend Engineer {job_id}</p>
                    <div class="meta">
                        <span>서울 강남구</span><span>경력5년↑</span><span>대졸↑</span>
                    </div>
                    <div class="corp">
                        <span class="corp_name">테스트컴퍼니</span>
                    </div>
                </div>
            </a>
        </div>
        """
    )

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



def _detail_html(*, job_id: str, encoded_body: str, detail_pairs: tuple[tuple[str, str], ...] = ()) -> str:
    detail_html = "".join(
        f'<dt class="tit">{label}</dt><dd class="desc">{value}</dd>'
        for label, value in detail_pairs
    )
    return textwrap.dedent(f"""\
        <html>
        <body>
        <dl>{detail_html}</dl>
        <script>
            var detailContents_{job_id} = {{
                contents: '{encoded_body}',
                mobile_contents_yn: ''
            }};
        </script>
        </body>
        </html>
    """)


def _encode_body(html: str) -> str:
    return base64.b64encode(html.encode(encoding="utf-8")).decode(encoding="utf-8")


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

    def test_extract_jd_body_separates_list_items(self) -> None:
        html = _detail_html(
            job_id="123",
            encoded_body=_encode_body(
                "<p>■ 자격요건</p><ul><li>Python 백엔드 개발 경험</li><li>SQL 활용 능력</li></ul>"
            ),
        )

        body = extract_jd_body(html, "123")

        assert body.splitlines() == [
            "■ 자격요건",
            "- Python 백엔드 개발 경험",
            "- SQL 활용 능력",
        ]


class TestSaraminAdapter:
    def test_supports_search(self) -> None:
        adapter = SaraminAdapter()
        assert adapter.supports_search is True
        assert adapter.name == "saramin"

    def test_search_returns_candidates(self) -> None:
        http = StubSearchHttp([
            {"count": "3", "innerHTML": SEARCH_CARD_HTML},
            {"count": "3", "innerHTML": ""},
        ])
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
        http = StubSearchHttp([
            {"count": "3", "innerHTML": SEARCH_CARD_HTML},
            {"count": "3", "innerHTML": ""},
        ])
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

    def test_search_serializes_configured_experience_range_on_first_and_next_pages(self, monkeypatch) -> None:
        http = StubSearchHttp([
            {"count": "4", "innerHTML": SEARCH_CARD_HTML},
            {"count": "4", "innerHTML": SEARCH_CARD_HTML_PAGE_2},
            {"count": "4", "innerHTML": ""},
        ])
        config = _config(http, api_min_experience=5, api_max_experience=10)
        config.rate_limits = {"saramin": 1.5}
        sleeps: list[float] = []
        monkeypatch.setattr("careerkit.jobs.adapters.platforms.saramin.time.sleep", sleeps.append)
        result = SaraminAdapter().search(
            "백엔드",
            config=config,
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

        assert parse_qs(urlparse(http.urls[2]).query)["page"] == ["3"]
        assert sleeps == [1.5, 1.5]
        for url in http.urls:
            query = parse_qs(urlparse(url).query)
            assert query["exp_cd"] == ["2"]
            assert query["exp_min"] == ["5"]
            assert query["exp_max"] == ["10"]
            assert query["exp_none"] == ["y"]

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
        assert result.stop_reason == "request_error"
        assert result.total_count == 4
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

    def test_search_fetches_past_five_pages_until_empty_api_page(self) -> None:
        http = StubSearchHttp([
            {"count": "6", "innerHTML": _search_card_html(job_id)}
            for job_id in range(1, 7)
        ] + [{"count": "6", "innerHTML": ""}])

        result = SaraminAdapter().search(
            "백엔드", config=_config(http), state=None, http=http
        )

        assert [item.job_id for item in result.items] == [str(job_id) for job_id in range(1, 7)]
        assert result.pages_fetched == 7
        assert result.total_count == 6
        assert result.complete is True
        assert result.stop_reason == "api_end"
        assert [parse_qs(urlparse(url).query)["page"] for url in http.urls] == [
            [str(page)] for page in range(1, 8)
        ]

    def test_search_treats_count_as_advisory_and_keeps_collecting(self) -> None:
        http = StubSearchHttp([
            {"count": "1", "innerHTML": _search_card_html(1)},
            {"count": "1", "innerHTML": _search_card_html(2)},
            {"count": "1", "innerHTML": ""},
        ])

        result = SaraminAdapter().search(
            "백엔드", config=_config(http), state=None, http=http
        )

        assert [item.job_id for item in result.items] == ["1", "2"]
        assert result.pages_fetched == 3
        assert result.total_count == 1
        assert result.stop_reason == "api_end"

    def test_search_returns_none_total_count_for_missing_invalid_or_conflicting_counts(self) -> None:
        http = StubSearchHttp([
            {"innerHTML": _search_card_html(1)},
            {"count": "not-a-number", "innerHTML": _search_card_html(2)},
            {"innerHTML": ""},
        ])
        result = SaraminAdapter().search(
            "백엔드", config=_config(http), state=None, http=http
        )
        assert [item.job_id for item in result.items] == ["1", "2"]
        assert result.pages_fetched == 3
        assert result.stop_reason == "api_end"
        assert result.total_count is None

        for first_count, second_count in [(-1, -1), ("1", "2"), ("²", "²")]:
            http = StubSearchHttp([
                {"count": first_count, "innerHTML": _search_card_html(1)},
                {"count": second_count, "innerHTML": _search_card_html(2)},
                {"count": second_count, "innerHTML": ""},
            ])
            result = SaraminAdapter().search(
                "백엔드", config=_config(http), state=None, http=http
            )
            assert [item.job_id for item in result.items] == ["1", "2"]
            assert result.pages_fetched == 3
            assert result.total_count is None

    def test_search_preserves_integer_total_count(self) -> None:
        http = StubSearchHttp([
            {"count": 2, "innerHTML": _search_card_html(1)},
            {"count": 2, "innerHTML": ""},
        ])

        result = SaraminAdapter().search(
            "백엔드", config=_config(http), state=None, http=http
        )

        assert result.total_count == 2

    def test_search_normalizes_valid_count_strings(self) -> None:
        http = StubSearchHttp([
            {"count": " 1,234 ", "innerHTML": _search_card_html(1)},
            {"count": "1234", "innerHTML": ""},
        ])

        result = SaraminAdapter().search(
            "백엔드", config=_config(http), state=None, http=http
        )

        assert result.total_count == 1234

    def test_search_returns_malformed_response_for_invalid_envelopes(self) -> None:
        responses = [
            None,
            [],
            {"count": "1"},
            {"count": "1", "innerHTML": None},
            {"count": "1", "innerHTML": 123},
        ]
        for response in responses:
            http = StubSearchHttp([response])
            result = SaraminAdapter().search(
                "백엔드", config=_config(http), state=None, http=http
            )
            assert result.items == ()
            assert result.pages_fetched == 1
            assert result.complete is False
            assert result.stop_reason == "malformed_response"
            assert result.total_count is None

    def test_search_returns_malformed_page_for_non_empty_unparseable_html(self) -> None:
        http = StubSearchHttp([{"count": "1", "innerHTML": "<div>no cards</div>"}])

        result = SaraminAdapter().search(
            "백엔드", config=_config(http), state=None, http=http
        )

        assert result.items == ()
        assert result.complete is False
        assert result.stop_reason == "malformed_page"

    def test_search_stops_on_repeated_page_and_keeps_partial_items(self) -> None:
        http = StubSearchHttp([
            {"count": "2", "innerHTML": _search_card_html(1)},
            {"count": "2", "innerHTML": _search_card_html(1)},
        ])

        result = SaraminAdapter().search(
            "백엔드", config=_config(http), state=None, http=http
        )

        assert [item.job_id for item in result.items] == ["1"]
        assert result.pages_fetched == 2
        assert result.complete is False
        assert result.stop_reason == "repeated_page"

    def test_search_keeps_new_ids_from_partial_overlap(self) -> None:
        http = StubSearchHttp([
            {"count": "3", "innerHTML": _search_card_html(1) + _search_card_html(2)},
            {"count": "3", "innerHTML": _search_card_html(2) + _search_card_html(3)},
            {"count": "3", "innerHTML": ""},
        ])

        result = SaraminAdapter().search(
            "백엔드", config=_config(http), state=None, http=http
        )

        assert [item.job_id for item in result.items] == ["1", "2", "3"]
        assert result.stop_reason == "api_end"

    def test_search_stops_when_page_has_no_new_ids(self) -> None:
        http = StubSearchHttp([
            {"count": "2", "innerHTML": _search_card_html(1) + _search_card_html(2)},
            {"count": "2", "innerHTML": _search_card_html(2) + _search_card_html(1)},
        ])

        result = SaraminAdapter().search(
            "백엔드", config=_config(http), state=None, http=http
        )

        assert [item.job_id for item in result.items] == ["1", "2"]
        assert result.pages_fetched == 2
        assert result.complete is False
        assert result.stop_reason == "no_new_items"

    def test_search_reraises_first_page_request_error(self) -> None:
        http = StubSearchHttp([RuntimeError("page 1 unavailable")])

        with pytest.raises(RuntimeError, match="page 1 unavailable"):
            SaraminAdapter().search("백엔드", config=_config(http), state=None, http=http)

    def test_search_preserves_candidates_before_later_malformed_response(self) -> None:
        malformed_responses = [
            None,
            [],
            {"count": "2"},
            {"count": "2", "innerHTML": None},
            {"count": "2", "innerHTML": 123},
        ]
        for malformed_response in malformed_responses:
            http = StubSearchHttp([
                {"count": "2", "innerHTML": _search_card_html(1)},
                malformed_response,
            ])
            result = SaraminAdapter().search(
                "백엔드", config=_config(http), state=None, http=http
            )
            assert [item.job_id for item in result.items] == ["1"]
            assert result.pages_fetched == 2
            assert result.complete is False
            assert result.stop_reason == "malformed_response"

    def test_search_preserves_candidates_before_later_malformed_page(self) -> None:
        http = StubSearchHttp([
            {"count": "2", "innerHTML": _search_card_html(1)},
            {"count": "2", "innerHTML": "<div>no cards</div>"},
        ])

        result = SaraminAdapter().search(
            "백엔드", config=_config(http), state=None, http=http
        )

        assert [item.job_id for item in result.items] == ["1"]
        assert result.pages_fetched == 2
        assert result.complete is False
        assert result.stop_reason == "malformed_page"

    def test_search_stops_at_page_safety_limit(self) -> None:
        http = StubSearchHttp([
            {"count": "1000", "innerHTML": _search_card_html(job_id)}
            for job_id in range(1000)
        ])

        result = SaraminAdapter().search(
            "백엔드", config=_config(http), state=None, http=http
        )

        assert len(result.items) == 1000
        assert result.pages_fetched == 1000
        assert len(http.urls) == 1000
        assert parse_qs(urlparse(http.urls[-1]).query)["page"] == ["1000"]
        assert result.complete is False
        assert result.stop_reason == "safety_page_limit"

    def test_search_stops_at_time_safety_limit(self, monkeypatch) -> None:
        calls: list[None] = []

        def fake_monotonic() -> float:
            calls.append(None)
            return 0.0 if len(calls) <= 3 else 600.0

        monkeypatch.setattr("careerkit.jobs.adapters.platforms.saramin.time.monotonic", fake_monotonic)
        http = StubSearchHttp([
            {"count": "2", "innerHTML": _search_card_html(1)},
            {"count": "2", "innerHTML": _search_card_html(2)},
        ])

        result = SaraminAdapter().search(
            "백엔드", config=_config(http), state=None, http=http
        )

        assert [item.job_id for item in result.items] == ["1"]
        assert result.pages_fetched == 1
        assert result.complete is False
        assert result.stop_reason == "safety_time_limit"

    def test_search_reports_safety_after_slow_response(self, monkeypatch) -> None:
        calls: list[None] = []

        def fake_monotonic() -> float:
            calls.append(None)
            return 0.0 if len(calls) <= 2 else 600.0

        monkeypatch.setattr("careerkit.jobs.adapters.platforms.saramin.time.monotonic", fake_monotonic)
        http = StubSearchHttp([{"count": "0", "innerHTML": ""}])

        result = SaraminAdapter().search(
            "백엔드", config=_config(http), state=None, http=http
        )

        assert result.items == ()
        assert result.pages_fetched == 1
        assert result.complete is False
        assert result.stop_reason == "safety_time_limit"

    def test_search_bounds_request_timeout_to_deadline(self, monkeypatch) -> None:
        calls: list[None] = []

        def fake_monotonic() -> float:
            calls.append(None)
            return 0.0 if len(calls) == 1 else 599.0

        monkeypatch.setattr("careerkit.jobs.adapters.platforms.saramin.time.monotonic", fake_monotonic)
        http = StubSearchHttp([{"count": "0", "innerHTML": ""}])

        result = SaraminAdapter().search(
            "백엔드", config=_config(http), state=None, http=http
        )

        assert http.timeouts == [1]
        assert result.complete is True
        assert result.stop_reason == "api_end"

    def test_search_caps_rate_limit_sleep_to_remaining_deadline(self, monkeypatch) -> None:
        calls: list[None] = []
        sleeps: list[float] = []

        def fake_monotonic() -> float:
            calls.append(None)
            return 0.0 if len(calls) <= 3 else 599.0

        monkeypatch.setattr("careerkit.jobs.adapters.platforms.saramin.time.monotonic", fake_monotonic)
        monkeypatch.setattr("careerkit.jobs.adapters.platforms.saramin.time.sleep", sleeps.append)
        http = StubSearchHttp([
            {"count": "1", "innerHTML": _search_card_html(1)},
            {"count": "1", "innerHTML": ""},
        ])
        config = _config(http)
        config.rate_limits = {"saramin": 10.0}

        result = SaraminAdapter().search(
            "백엔드", config=config, state=None, http=http
        )

        assert sleeps == [1.0]
        assert result.stop_reason == "api_end"


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
    def __init__(self, responses: str | Sequence[dict | Exception], count: str = "0") -> None:
        if isinstance(responses, list):
            self.responses = list(responses)
        else:
            self.responses = [{"count": count, "innerHTML": responses}]
        self.urls: list[str] = []
        self.timeouts: list[object] = []

    def request_json(self, url: str, **kwargs) -> dict:
        self.urls.append(url)
        self.timeouts.append(kwargs.get("timeout"))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def request_text(self, url: str, **kwargs) -> str:
        return ""


class TestSaraminSectionExtraction:
    def test_extract_jd_sections_splits_alias_headings_and_keeps_unclassified_prose(self) -> None:
        html = _detail_html(
            job_id="123",
            encoded_body=_encode_body(
                "<h2>담당업무</h2><ul><li>API 개발</li><li>장애 대응</li></ul>"
                "<h2>지원자격</h2><p>Python 경험</p><ul><li>FastAPI 경험</li></ul>"
                "<h2>우대조건</h2><p>AWS 경험</p>"
                "<h2>기타</h2><p>팀 소개</p>"
            ),
        )

        sections = extract_jd_sections(html, "123")

        assert sections.main_duties == ("- API 개발", "- 장애 대응")
        assert sections.requirements == ("Python 경험", "- FastAPI 경험")
        assert sections.preferred == ("- AWS 경험",)
        assert sections.introduction == ("팀 소개",)

    def test_extract_jd_sections_preserves_html_boundaries_before_tag_removal(self) -> None:
        html = _detail_html(
            job_id="123",
            encoded_body=_encode_body(
                "<h2>주요업무</h2><ul><li>서비스 운영</li><li>품질 개선</li></ul>"
                "<h2>자격요건</h2><p>문서화 역량</p><p>협업 역량</p>"
                "<h2>우대사항</h2><p>커뮤니케이션</p><br>테스트 자동화"
            ),
        )

        sections = extract_jd_sections(html, "123")

        assert sections.main_duties == ("- 서비스 운영", "- 품질 개선")
        assert sections.requirements == ("문서화 역량", "협업 역량")
        assert sections.preferred == ("- 커뮤니케이션", "- 테스트 자동화")

    def test_extract_jd_sections_reads_plain_text_icon_headings(self) -> None:
        html = _detail_html(
            job_id="123",
            encoded_body=_encode_body(
                "<div>모집분야</div>"
                "<div>📋 주요업무</div><div>• API 개발</div>"
                "<div>📋 자격요건</div><div>• Python 경험</div>"
                "<div>🏠 근무조건</div><div>• 정규직</div>"
            ),
        )

        sections = extract_jd_sections(html, "123")

        assert sections.main_duties == ("- API 개발",)
        assert sections.requirements == ("- Python 경험",)
        assert sections.preferred == ()

    def test_extract_jd_sections_reads_plain_text_sections_inside_generic_headings(self) -> None:
        html = _detail_html(
            job_id="123",
            encoded_body=_encode_body(
                "<h2>모집부문 / 상세내용</h2>"
                "<div>업무내용</div><div>• API 개발</div>"
                "<div>필요 역량/경험</div><div>• Python 경험</div>"
                "<div>선호 역량/경험</div><div>• AWS 경험</div>"
                "<div>마감일 및 근무지</div><div>2026년 8월 31일</div>"
                "<h2>복지 및 혜택</h2><div>식대 지원</div>"
            ),
        )

        sections = extract_jd_sections(html, "123")

        assert sections.main_duties == ("- API 개발",)
        assert sections.requirements == ("- Python 경험",)
        assert sections.preferred == ("- AWS 경험",)



    def test_extract_jd_sections_drops_marker_only_lines(self) -> None:
        html = _detail_html(
            job_id="123",
            encoded_body=_encode_body(
                "<div>자격요건</div><div>•</div><div>Python 경험</div>"
                "<div>우대사항</div><div>-</div><div>AWS 경험</div>"
            ),
        )

        sections = extract_jd_sections(html, "123")

        assert sections.requirements == ("- Python 경험",)
        assert sections.preferred == ("- AWS 경험",)

    def test_extract_jd_sections_drops_repeated_and_unspaced_markers(self) -> None:
        html = _detail_html(
            job_id="123",
            encoded_body=_encode_body(
                "<div>자격요건</div><div>•Python 경험</div><div>- • AWS 경험</div>"
            ),
        )

        sections = extract_jd_sections(html, "123")

        assert sections.requirements == ("- Python 경험", "- AWS 경험")

    def test_extract_detail_sections_drops_marker_only_rows(self) -> None:
        html = _detail_html(
            job_id="123",
            encoded_body=_encode_body("<p>ignored</p>"),
            detail_pairs=(("자격요건", "<ul><li></li><li>Python 경험</li></ul>"),),
        )

        sections = extract_detail_sections(html)

        assert sections.requirements == ("- Python 경험",)

    def test_extract_jd_sections_normalizes_bracketed_parenthesized_and_numbered_headings(self) -> None:
        html = _detail_html(
            job_id="123",
            encoded_body=_encode_body(
                "<h2>[자격요건]</h2><ul><li>Python</li></ul>"
                "<h2>(우대사항)</h2><p>AWS</p>"
                "<h2>2. 담당업무</h2><ul><li>서비스 개발</li></ul>"
            ),
        )

        sections = extract_jd_sections(html, "123")

        assert sections.main_duties == ("- 서비스 개발",)
        assert sections.requirements == ("- Python",)
        assert sections.preferred == ("- AWS",)

    def test_extract_detail_sections_normalizes_bracketed_parenthesized_and_numbered_labels(self) -> None:
        html = _detail_html(
            job_id="123",
            encoded_body=_encode_body("<p>ignored</p>"),
            detail_pairs=(
                ("[자격요건]", "Python"),
                ("(우대사항)", "AWS"),
                ("2. 담당업무", "서비스 개발"),
            ),
        )

        sections = extract_detail_sections(html)

        assert sections.main_duties == ("- 서비스 개발",)
        assert sections.requirements == ("- Python",)
        assert sections.preferred == ("- AWS",)

    def test_extract_jd_sections_normalizes_combined_numbered_and_wrapped_headings(self) -> None:
        html = _detail_html(
            job_id="123",
            encoded_body=_encode_body(
                "<h2>1. [자격요건]</h2><ul><li>Python</li></ul>"
                "<h2>1) (우대사항)</h2><p>AWS</p>"
            ),
        )

        sections = extract_jd_sections(html, "123")

        assert sections.requirements == ("- Python",)
        assert sections.preferred == ("- AWS",)

    def test_extract_jd_sections_ignores_inline_strong_or_b_in_requirement_content(self) -> None:
        html = _detail_html(
            job_id="123",
            encoded_body=_encode_body(
                "<h2>자격요건</h2><ul><li><strong>필수</strong> Python 경험</li></ul>"
            ),
        )

        sections = extract_jd_sections(html, "123")

        assert sections.requirements == ("- 필수 Python 경험",)
        assert sections.introduction == ()

    def test_extract_jd_sections_missing_or_invalid_body_returns_empty_sections(self) -> None:
        missing = extract_jd_sections("<html></html>", "123")
        invalid = extract_jd_sections(_detail_html(job_id="123", encoded_body="bm90IGJhc2U2NA=="), "123")

        assert missing.main_duties == ()
        assert missing.requirements == ()
        assert missing.preferred == ()
        assert missing.introduction == ()
        assert invalid == missing

    def test_extract_detail_sections_maps_detail_fields_to_sections(self) -> None:
        html = _detail_html(
            job_id="123",
            encoded_body=_encode_body("<p>ignored</p>"),
            detail_pairs=(
                ("담당업무", "API 개발<br>장애 대응"),
                ("자격요건", "Python 경험<br>FastAPI 경험"),
                ("우대사항", "AWS 경험"),
            ),
        )

        sections = extract_detail_sections(html)

        assert sections.main_duties == ("- API 개발", "- 장애 대응")
        assert sections.requirements == ("- Python 경험", "- FastAPI 경험")
        assert sections.preferred == ("- AWS 경험",)
        assert sections.introduction == ()
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
