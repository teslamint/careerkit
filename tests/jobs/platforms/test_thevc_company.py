from __future__ import annotations

import json

import pytest

from careerkit.jobs.adapters.http import HttpStatusError
from careerkit.jobs.adapters.platforms.thevc import (
    TheVCCompanyInfo,
    TheVCFundingRound,
    _is_paywall,
    format_thevc_company_markdown,
    thevc_company_http,
)


SAMPLE_API_RESPONSE = {
    "name": "포지큐브",
    "nameEn": "POSICUBE",
    "foundedOn": "2017-05-22T15:00:00.000Z",
    "corpType": "주식회사",
    "status": "비상장",
    "address": "서울특별시 강남구 역삼로7길 5",
    "website": "http://posicube.com",
    "members": [
        {
            "name": "오성조",
            "position": "대표이사",
            "isCEO": True,
            "isFounder": True,
        }
    ],
    "relatedKeywords": ["AI기술", "인공지능", "고객센터"],
    "products": [
        {"name": "robi리셉션", "desc": "AI 기반 고객센터 솔루션"},
        {"name": "AI에이전트", "desc": ""},
    ],
    "lastRound": "Series B",
    "lastFundedOn": "2021-11-08T15:00:00.000Z",
    "totalFundingCount": 4,
    "investorCount": {"total": 9, "person": 0, "organization": 9},
    "fundings": [
        {
            "fundedOn": "2020-06-15T15:00:00.000Z",
            "round": "Series A",
            "type": "시리즈 A",
            "totalAmount": 19646,
            "investors": [{"body": {"name": {"requirements": ["PLAN:BASIC"]}}}],
        },
        {
            "fundedOn": "2021-11-08T15:00:00.000Z",
            "round": "Series B",
            "type": "시리즈 B",
            "totalAmount": 61745,
            "investors": [{"body": {"name": {"requirements": ["PLAN:BASIC"]}}}],
        },
    ],
}


class _FakeHttpClient:
    def __init__(self, response=None, *, status_error: int | None = None):
        self._response = response
        self._status_error = status_error
        self.last_url: str | None = None

    def request_json(self, url, **_kwargs):
        self.last_url = url
        if self._status_error is not None:
            raise HttpStatusError(f"HTTP {self._status_error}", status=self._status_error)
        if self._response is None:
            return {}
        return self._response

    def request_text(self, url, **_kwargs):
        self.last_url = url
        if self._status_error is not None:
            raise HttpStatusError(f"HTTP {self._status_error}", status=self._status_error)
        return json.dumps(self._response or {})


class TestIsPaywall:
    def test_paywall_dict(self):
        assert _is_paywall({"requirements": ["PLAN:PRO"]})

    def test_normal_dict(self):
        assert not _is_paywall({"total": 9})

    def test_string(self):
        assert not _is_paywall("hello")

    def test_none(self):
        assert not _is_paywall(None)

    def test_list(self):
        assert not _is_paywall(["PLAN:PRO"])


class TestThevcCompanyHttp:
    def test_parses_basic_info(self):
        client = _FakeHttpClient(SAMPLE_API_RESPONSE)
        info = thevc_company_http("posicube", http=client)

        assert info.name == "포지큐브"
        assert info.name_en == "POSICUBE"
        assert info.founded_on == "2017-05-23"
        assert info.corp_type == "주식회사"
        assert info.status == "비상장"
        assert info.address == "서울특별시 강남구 역삼로7길 5"
        assert info.website == "http://posicube.com"
        assert info.slug == "posicube"

    def test_parses_ceo(self):
        client = _FakeHttpClient(SAMPLE_API_RESPONSE)
        info = thevc_company_http("posicube", http=client)

        assert info.ceo_name == "오성조"
        assert info.ceo_is_founder is True

    def test_parses_funding_rounds(self):
        client = _FakeHttpClient(SAMPLE_API_RESPONSE)
        info = thevc_company_http("posicube", http=client)

        assert info.last_round == "Series B"
        assert info.last_funded_on == "2021-11-09"
        assert info.total_funding_count == 4
        assert len(info.funding_rounds) == 2
        assert info.funding_rounds[0].round_name == "Series A"
        assert info.funding_rounds[1].round_name == "Series B"

    def test_parses_investor_count(self):
        client = _FakeHttpClient(SAMPLE_API_RESPONSE)
        info = thevc_company_http("posicube", http=client)

        assert info.investor_count_total == 9

    def test_investor_count_paywall_yields_zero(self):
        response = {**SAMPLE_API_RESPONSE, "investorCount": {"requirements": ["PLAN:PRO"]}}
        client = _FakeHttpClient(response)
        info = thevc_company_http("posicube", http=client)

        assert info.investor_count_total == 0

    def test_parses_keywords_and_products(self):
        client = _FakeHttpClient(SAMPLE_API_RESPONSE)
        info = thevc_company_http("posicube", http=client)

        assert info.keywords == ("AI기술", "인공지능", "고객센터")
        assert "robi리셉션 — AI 기반 고객센터 솔루션" in info.products
        assert "AI에이전트" in info.products

    def test_404_raises_value_error(self):
        client = _FakeHttpClient(status_error=404)
        with pytest.raises(ValueError, match="not found"):
            thevc_company_http("unknown-slug", http=client)

    def test_403_raises_http_status_error(self):
        client = _FakeHttpClient(status_error=403)
        with pytest.raises(HttpStatusError):
            thevc_company_http("blocked", http=client)

    def test_url_encodes_slug(self):
        client = _FakeHttpClient({})
        thevc_company_http("some/slug", http=client)
        assert "some%2Fslug" in (client.last_url or "")

    def test_empty_response(self):
        client = _FakeHttpClient({})
        info = thevc_company_http("empty", http=client)

        assert info.name == ""
        assert info.funding_rounds == ()
        assert info.investor_count_total == 0

    def test_paywall_fields_treated_as_none(self):
        response = {
            "name": "Test",
            "foundedOn": {"requirements": ["PLAN:PRO"]},
            "website": {"requirements": ["PLAN:FREE"]},
            "members": [],
        }
        client = _FakeHttpClient(response)
        info = thevc_company_http("test", http=client)

        assert info.founded_on == ""
        assert info.website == ""

    def test_paywall_total_funding_count_yields_zero(self):
        response = {**SAMPLE_API_RESPONSE, "totalFundingCount": {"requirements": ["PLAN:FREE"]}}
        client = _FakeHttpClient(response)
        info = thevc_company_http("test", http=client)

        assert info.total_funding_count == 0

    def test_paywall_keywords_yields_empty(self):
        response = {**SAMPLE_API_RESPONSE, "relatedKeywords": {"requirements": ["PLAN:FREE"]}}
        client = _FakeHttpClient(response)
        info = thevc_company_http("test", http=client)

        assert info.keywords == ()

    def test_date_kst_adjustment(self):
        response = {**SAMPLE_API_RESPONSE, "foundedOn": "2017-05-22T15:00:00.000Z"}
        client = _FakeHttpClient(response)
        info = thevc_company_http("test", http=client)

        assert info.founded_on == "2017-05-23"

    def test_non_dict_response_raises_value_error(self):
        client = _FakeHttpClient([])
        with pytest.raises(ValueError, match="unexpected response"):
            thevc_company_http("test", http=client)


class TestFormatThevcCompanyMarkdown:
    def test_contains_investment_heading(self):
        info = TheVCCompanyInfo(
            name="테스트회사",
            last_round="Series A",
            total_funding_count=2,
            funding_rounds=(
                TheVCFundingRound(round_name="Seed", funded_on="2020-01-01", funding_type="시드"),
                TheVCFundingRound(round_name="Series A", funded_on="2021-06-01", funding_type="시리즈 A"),
            ),
            slug="test-company",
        )
        md = format_thevc_company_markdown(info)

        assert "## 투자 정보" in md
        assert "| 현재 라운드 | Series A |" in md

    def test_no_amount_in_output(self):
        client = _FakeHttpClient(SAMPLE_API_RESPONSE)
        info = thevc_company_http("posicube", http=client)
        md = format_thevc_company_markdown(info)

        assert "19646" not in md
        assert "61745" not in md
        assert "totalAmount" not in md

    def test_source_url(self):
        info = TheVCCompanyInfo(name="Test", slug="test-slug")
        md = format_thevc_company_markdown(info)

        assert "https://thevc.kr/test-slug" in md

    def test_startup_marker(self):
        info = TheVCCompanyInfo(name="Test", slug="test")
        md = format_thevc_company_markdown(info)

        assert "| 스타트업 여부 | yes |" in md

    def test_no_investment_section_without_rounds(self):
        info = TheVCCompanyInfo(name="Test", slug="test")
        md = format_thevc_company_markdown(info)

        assert "## 투자 정보" not in md

    def test_funding_table_columns(self):
        info = TheVCCompanyInfo(
            name="Test",
            last_round="Seed",
            funding_rounds=(
                TheVCFundingRound(round_name="Seed", funded_on="2020-01-01", funding_type="시드"),
            ),
            slug="test",
        )
        md = format_thevc_company_markdown(info)

        assert "| 라운드 | 날짜 | 유형 |" in md
        assert "| Seed | 2020-01-01 | 시드 |" in md
