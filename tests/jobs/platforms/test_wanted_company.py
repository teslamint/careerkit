from __future__ import annotations

import json

import pytest

from careerkit.jobs.adapters.platforms.wanted import (
    format_wanted_company_markdown,
    wanted_company_http,
    wanted_company_matches,
    wanted_search_company_id,
)


def _make_wanted_company_html(info: dict, summary: dict) -> str:
    queries = [
        {"queryKey": ["companyInfo", "12345"], "state": {"data": info}},
        {"queryKey": ["companySummary", "abc123"], "state": {"data": summary}},
        {"queryKey": ["companyHire", "12345"], "state": {"data": {"jobs": []}}},
    ]
    payload = {
        "props": {
            "pageProps": {
                "dehydrateState": {"queries": queries},
            },
        },
    }
    return f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>'


SAMPLE_INFO = {
    "name": "테스트랩스(커넥팅)",
    "industryName": "IT, 컨텐츠",
    "foundedYear": 2018,
    "age": 8,
    "status": "RUNNING",
    "location": "서울 강남구",
    "mainTags": [{"id": 1, "title": "50명이하", "tag_category_id": 3}],
    "companyTags": [
        {"id": 2, "title": "스톡옵션", "tag_category_id": 7},
        {"id": 1, "title": "50명이하", "tag_category_id": 3},
    ],
    "description": "글로벌 소셜 플랫폼",
    "link": "https://example.com",
    "address": {"full_location": "서울 강남구 언주로 540"},
}

SAMPLE_SUMMARY = {
    "detail": {
        "foundedYear": 2018,
        "npsEmployeeCount": 24,
        "totalSales": 3833582000,
        "salary": 48002444,
        "hiredCount": 15,
        "leftCount": 13,
    },
    "salary": {"salary": 48002444, "source": "NPS"},
    "employee": {"total": 24, "hired": 15, "left": 13},
    "sales": {"total": 3833582000, "source": "KODATA", "updatedAt": "2023-12-31"},
}


class _FakeHttp:
    def __init__(self, text: str):
        self._text = text

    def request_text(self, url: str, **kw) -> str:
        return self._text

    def request_json(self, url: str, **kw) -> dict:
        raise NotImplementedError


class TestWantedCompanyHttp:
    def test_parses_full_data(self):
        html = _make_wanted_company_html(SAMPLE_INFO, SAMPLE_SUMMARY)
        info = wanted_company_http(12345, http=_FakeHttp(html))

        assert info.company_id == 12345
        assert info.name == "테스트랩스(커넥팅)"
        assert info.industry == "IT, 컨텐츠"
        assert info.founded_year == 2018
        assert info.location == "서울 강남구 언주로 540"
        assert info.employee_count == 24
        assert info.avg_salary_manwon == 4800
        assert info.hired_1y == 15
        assert info.left_1y == 13
        assert info.total_sales_eok == 38.3
        assert info.sales_year == "2023"
        assert "50명이하" in info.tags
        assert "스톡옵션" in info.tags
        assert info.description == "글로벌 소셜 플랫폼"
        assert info.homepage == "https://example.com"

    def test_tags_deduplicated(self):
        html = _make_wanted_company_html(SAMPLE_INFO, SAMPLE_SUMMARY)
        info = wanted_company_http(12345, http=_FakeHttp(html))
        assert info.tags.count("50명이하") == 1

    def test_salary_won_to_manwon(self):
        html = _make_wanted_company_html(SAMPLE_INFO, SAMPLE_SUMMARY)
        info = wanted_company_http(12345, http=_FakeHttp(html))
        assert info.avg_salary_manwon == 4800

    def test_sales_won_to_eok(self):
        html = _make_wanted_company_html(SAMPLE_INFO, SAMPLE_SUMMARY)
        info = wanted_company_http(12345, http=_FakeHttp(html))
        assert info.total_sales_eok == 38.3

    def test_missing_next_data_raises(self):
        with pytest.raises(ValueError, match="missing __NEXT_DATA__"):
            wanted_company_http(12345, http=_FakeHttp("<html></html>"))

    def test_missing_company_info_query_raises(self):
        payload = {"props": {"pageProps": {"dehydrateState": {"queries": []}}}}
        html = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>'
        with pytest.raises(ValueError, match="companyInfo"):
            wanted_company_http(12345, http=_FakeHttp(html))

    def test_empty_summary_produces_none_fields(self):
        html = _make_wanted_company_html(SAMPLE_INFO, {})
        info = wanted_company_http(12345, http=_FakeHttp(html))
        assert info.name == "테스트랩스(커넥팅)"
        assert info.avg_salary_manwon is None
        assert info.total_sales_eok is None
        assert info.hired_1y is None

    def test_zero_salary_preserves_zero(self):
        summary = {
            "detail": {},
            "salary": {"salary": 0, "source": "NPS"},
            "employee": {"total": 11, "hired": 3, "left": 0},
            "sales": {"total": 0, "source": "KODATA", "updatedAt": "2024-12-31"},
        }
        html = _make_wanted_company_html(SAMPLE_INFO, summary)
        info = wanted_company_http(12345, http=_FakeHttp(html))
        assert info.avg_salary_manwon == 0
        assert info.total_sales_eok == 0.0

    def test_zero_left_preserves_zero(self):
        summary = dict(SAMPLE_SUMMARY)
        summary["employee"] = {"total": 11, "hired": 3, "left": 0}
        html = _make_wanted_company_html(SAMPLE_INFO, summary)
        info = wanted_company_http(12345, http=_FakeHttp(html))
        assert info.left_1y == 0

    def test_string_company_id_coerced(self):
        html = _make_wanted_company_html(SAMPLE_INFO, SAMPLE_SUMMARY)
        info = wanted_company_http("12345", http=_FakeHttp(html))
        assert info.company_id == 12345


class TestFormatWantedCompanyMarkdown:
    def test_contains_required_sections(self):
        html = _make_wanted_company_html(SAMPLE_INFO, SAMPLE_SUMMARY)
        info = wanted_company_http(12345, http=_FakeHttp(html))
        md = format_wanted_company_markdown(info)
        assert "# 테스트랩스(커넥팅)" in md
        assert "4,800만원" in md
        assert "24명" in md
        assert "38.3억원" in md
        assert "2018년" in md


class TestWantedCompanyMatches:
    def test_exact_name_and_industry_match_returns_true(self):
        html = _make_wanted_company_html(SAMPLE_INFO, SAMPLE_SUMMARY)
        info = wanted_company_http(12345, http=_FakeHttp(html))

        assert wanted_company_matches(
            info,
            "테스트랩스(커넥팅)",
            verify_industry="IT",
        ) is True

    def test_location_only_corroboration_returns_true(self):
        html = _make_wanted_company_html(SAMPLE_INFO, SAMPLE_SUMMARY)
        info = wanted_company_http(12345, http=_FakeHttp(html))

        assert wanted_company_matches(
            info,
            "테스트랩스(커넥팅)",
            verify_location="서울 강남구",
        ) is True

    def test_name_mismatch_returns_false(self):
        html = _make_wanted_company_html(SAMPLE_INFO, SAMPLE_SUMMARY)
        info = wanted_company_http(12345, http=_FakeHttp(html))

        assert wanted_company_matches(
            info,
            "다른회사",
            verify_industry="IT",
        ) is False

    def test_unsafe_detail_text_returns_false(self):
        unsafe_info = dict(SAMPLE_INFO)
        unsafe_info["industryName"] = "IT|보안"
        html = _make_wanted_company_html(unsafe_info, SAMPLE_SUMMARY)
        info = wanted_company_http(12345, http=_FakeHttp(html))

        assert wanted_company_matches(
            info,
            "테스트랩스(커넥팅)",
            verify_industry="IT",
        ) is False

    def test_invalid_metrics_return_false(self):
        bad_summary = dict(SAMPLE_SUMMARY)
        bad_summary["detail"] = dict(SAMPLE_SUMMARY["detail"], foundedYear=1799)
        bad_summary["salary"] = {"salary": 10_000_010_000, "source": "NPS"}
        html = _make_wanted_company_html(SAMPLE_INFO, bad_summary)
        info = wanted_company_http(12345, http=_FakeHttp(html))

        assert wanted_company_matches(
            info,
            "테스트랩스(커넥팅)",
            verify_industry="IT",
        ) is False


class TestWantedSearchCompanyId:
    def _make_search_html(self, results: list[dict]) -> str:
        queries = [
            {
                "queryKey": ["searchCompany", "테스트"],
                "state": {"data": {"data": results}},
            }
        ]
        payload = {
            "props": {
                "pageProps": {
                    "dehydrateState": {"queries": queries},
                },
            },
        }
        return f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>'

    def test_exact_match_returns_id(self):
        html = self._make_search_html([
            {"id": 12345, "name": "테스트랩스", "industry_name": "IT", "location": "서울"},
        ])
        result = wanted_search_company_id(
            "테스트랩스", verify_industry="IT", http=_FakeHttp(html)
        )
        assert result == 12345

    def test_name_mismatch_returns_none(self):
        html = self._make_search_html([
            {"id": 9999, "name": "다른회사", "industry_name": "IT", "location": "서울"},
        ])
        result = wanted_search_company_id(
            "테스트랩스", verify_industry="IT", http=_FakeHttp(html)
        )
        assert result is None

    def test_no_corroboration_returns_none(self):
        html = self._make_search_html([
            {"id": 12345, "name": "테스트랩스", "industry_name": "금융", "location": "부산"},
        ])
        result = wanted_search_company_id(
            "테스트랩스", verify_industry="IT", verify_location="서울", http=_FakeHttp(html)
        )
        assert result is None

    def test_no_verify_fields_rejects_name_only_match(self):
        html = self._make_search_html([
            {"id": 12345, "name": "테스트랩스"},
        ])
        result = wanted_search_company_id("테스트랩스", http=_FakeHttp(html))
        assert result is None

    def test_http_failure_returns_none(self):
        class _FailHttp:
            def request_text(self, url, **kw):
                raise OSError("network down")

            def request_json(self, url, **kw):
                raise NotImplementedError

        result = wanted_search_company_id("테스트랩스", http=_FailHttp())
        assert result is None

    def test_empty_results_returns_none(self):
        html = self._make_search_html([])
        result = wanted_search_company_id("테스트랩스", http=_FakeHttp(html))
        assert result is None

    def test_location_only_match_returns_id(self):
        html = self._make_search_html([
            {"id": 12345, "name": "테스트랩스", "location": "서울"},
        ])
        result = wanted_search_company_id(
            "테스트랩스", verify_location="서울", http=_FakeHttp(html)
        )
        assert result == 12345

    def test_industry_wrong_but_location_right_returns_id(self):
        html = self._make_search_html([
            {"id": 12345, "name": "테스트랩스", "industry_name": "금융", "location": "서울"},
        ])
        result = wanted_search_company_id(
            "테스트랩스", verify_industry="IT", verify_location="서울", http=_FakeHttp(html)
        )
        assert result == 12345

    def test_industry_only_no_match_returns_none(self):
        html = self._make_search_html([
            {"id": 12345, "name": "테스트랩스", "industry_name": "금융"},
        ])
        result = wanted_search_company_id(
            "테스트랩스", verify_industry="IT", http=_FakeHttp(html)
        )
        assert result is None

    def test_industry_substring_false_positive_returns_none(self):
        html = self._make_search_html([
            {"id": 12345, "name": "테스트랩스", "industry_name": "Retail"},
        ])
        result = wanted_search_company_id(
            "테스트랩스", verify_industry="AI", http=_FakeHttp(html)
        )
        assert result is None

    def test_camelcase_industry_key_accepted(self):
        html = self._make_search_html([
            {"id": 12345, "name": "테스트랩스", "industryName": "IT"},
        ])
        result = wanted_search_company_id(
            "테스트랩스", verify_industry="IT", http=_FakeHttp(html)
        )
        assert result == 12345

    def test_skips_first_non_match_finds_second(self):
        html = self._make_search_html([
            {"id": 1111, "name": "다른회사", "industry_name": "IT"},
            {"id": 12345, "name": "테스트랩스", "industry_name": "IT"},
        ])
        result = wanted_search_company_id(
            "테스트랩스", verify_industry="IT", http=_FakeHttp(html)
        )
        assert result == 12345

    def test_hierarchical_location_match_returns_id(self):
        html = self._make_search_html([
            {"id": 12345, "name": "테스트랩스", "location": "서울 강남구 언주로 540"},
        ])
        result = wanted_search_company_id(
            "테스트랩스", verify_location="서울 강남구", http=_FakeHttp(html)
        )
        assert result == 12345

    def test_malformed_result_name_returns_none_without_raising(self):
        html = self._make_search_html([
            {"id": 12345, "name": 1234, "industry_name": "IT"},
        ])
        result = wanted_search_company_id(
            "테스트랩스", verify_industry="IT", http=_FakeHttp(html)
        )
        assert result is None

    def test_malformed_result_id_returns_none_without_raising(self):
        html = self._make_search_html([
            {"id": "abc", "name": "테스트랩스", "industry_name": "IT"},
        ])
        result = wanted_search_company_id(
            "테스트랩스", verify_industry="IT", http=_FakeHttp(html)
        )
        assert result is None

    def test_query_key_none_skips_record(self):
        payload = {
            "props": {
                "pageProps": {
                    "dehydrateState": {
                        "queries": [
                            {"queryKey": None, "state": {"data": {"data": []}}},
                            {
                                "queryKey": ["searchCompany", "테스트"],
                                "state": {"data": {"data": [{"id": 12345, "name": "테스트랩스", "industry_name": "IT"}]}},
                            },
                        ]
                    }
                }
            }
        }
        html = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>'
        assert wanted_search_company_id("테스트랩스", verify_industry="IT", http=_FakeHttp(html)) == 12345

    def test_state_none_skips_record(self):
        payload = {
            "props": {
                "pageProps": {
                    "dehydrateState": {
                        "queries": [
                            {"queryKey": ["searchCompany", "테스트"], "state": None},
                            {
                                "queryKey": ["searchCompany", "테스트"],
                                "state": {"data": {"data": [{"id": 12345, "name": "테스트랩스", "industry_name": "IT"}]}},
                            },
                        ]
                    }
                }
            }
        }
        html = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>'
        assert wanted_search_company_id("테스트랩스", verify_industry="IT", http=_FakeHttp(html)) == 12345

    def test_malformed_nested_containers_return_none(self):
        payload = {
            "props": {"pageProps": {"dehydrateState": {"queries": [{"queryKey": ["searchCompany"], "state": {"data": []}}]}}}
        }
        html = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>'
        assert wanted_search_company_id("테스트랩스", verify_industry="IT", http=_FakeHttp(html)) is None

    def test_continues_after_malformed_result_row(self):
        html = self._make_search_html([
            {"id": "abc", "name": "테스트랩스", "industry_name": "IT"},
            {"id": 12345, "name": "테스트랩스", "industry_name": "IT"},
        ])
        result = wanted_search_company_id(
            "테스트랩스", verify_industry="IT", http=_FakeHttp(html)
        )
        assert result == 12345
