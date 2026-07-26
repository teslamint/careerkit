from __future__ import annotations

import json

import pytest

from careerkit.jobs.adapters.platforms._next_data import extract_next_data, find_query_by_key
from careerkit.jobs.adapters.platforms.remember import remember_company_http


def _make_next_data(queries: list[dict], *, reverse: bool = False) -> str:
    if reverse:
        queries = list(reversed(queries))
    payload = {
        "props": {
            "pageProps": {
                "dehydratedState": {"queries": queries},
            },
        },
    }
    return f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>'


def _company_query(company_id: int = 99999, **overrides: object) -> dict:
    company = {
        "id": company_id,
        "name": "(주)테스트컴퍼니",
        "address": "서울 강남구 테헤란로 1",
        "homepageUrl": "https://example.com",
        "businessCode": "1234567890",
        "industry": {"level1": "IT·통신", "level2": "SW/App", "level3": ""},
        "description": "테스트 회사입니다.",
        "representativeName": "홍길동",
        "type": "스타트업",
        "establishmentDate": "2022-01-15",
        "logoUrl": "https://example.com/logo.png",
        "tags": ["자유복장", "스톡옵션"],
        "salaryStatistics": {
            "average": 49799008,
            "changesFromLastYear": 1533675,
            "relatedCompaniesAverage": None,
        },
        "employeeStatistics": [
            {"month": "2026-06", "total": 60, "join": 12, "leave": 2},
            {"month": "2026-05", "total": 54, "join": 3, "leave": 6},
            {"month": "2026-04", "total": 55, "join": 2, "leave": 4},
        ],
        "relatedCompanies": [],
    }
    company.update(overrides)
    return {
        "queryKey": [f"/companies/{company_id}"],
        "state": {"data": {"data": company}},
    }


_USER_QUERY: dict = {
    "queryKey": ["/user"],
    "state": {"data": {"code": 401, "data": None, "errors": None, "message": "Unauthorized"}},
}


class FakeHttp:
    def __init__(self, html: str) -> None:
        self._html = html

    def request_text(self, url: str, **kwargs: object) -> str:
        return self._html

    def request_json(self, url: str, **kwargs: object) -> dict:
        raise NotImplementedError


def test_remember_company_http_extracts_all_fields() -> None:
    html = _make_next_data([_USER_QUERY, _company_query()])
    info = remember_company_http(99999, http=FakeHttp(html))

    assert info.company_id == 99999
    assert info.name == "(주)테스트컴퍼니"
    assert info.address == "서울 강남구 테헤란로 1"
    assert info.industry == "IT·통신 > SW/App"
    assert info.established == "2022-01-15"
    assert info.employee_count == 60
    assert info.avg_salary_manwon == 4980
    assert info.salary_yoy_change == 153
    assert len(info.employee_stats) == 3
    assert info.employee_stats[0]["month"] == "2026-06"
    assert info.company_type == "스타트업"
    assert info.homepage == "https://example.com"
    assert info.ceo == "홍길동"
    assert info.tags == ("자유복장", "스톡옵션")


def test_remember_company_http_querykey_selection_reversed_order() -> None:
    html = _make_next_data([_USER_QUERY, _company_query()], reverse=True)
    info = remember_company_http(99999, http=FakeHttp(html))

    assert info.company_id == 99999
    assert info.name == "(주)테스트컴퍼니"


def test_remember_company_http_partial_data_no_salary() -> None:
    html = _make_next_data([_USER_QUERY, _company_query(
        salaryStatistics=None,
        employeeStatistics=[],
    )])
    info = remember_company_http(99999, http=FakeHttp(html))

    assert info.avg_salary_manwon is None
    assert info.salary_yoy_change is None
    assert info.employee_count is None
    assert info.employee_stats == ()


def test_remember_company_http_partial_data_no_homepage() -> None:
    html = _make_next_data([_USER_QUERY, _company_query(homepageUrl=None)])
    info = remember_company_http(99999, http=FakeHttp(html))

    assert info.homepage == ""


def test_remember_company_http_salary_conversion_rounding() -> None:
    html = _make_next_data([_USER_QUERY, _company_query(
        salaryStatistics={"average": 50006000, "changesFromLastYear": -15333, "relatedCompaniesAverage": None},
    )])
    info = remember_company_http(99999, http=FakeHttp(html))

    assert info.avg_salary_manwon == 5001
    assert info.salary_yoy_change == -2


def test_remember_company_http_missing_company_raises() -> None:
    html = '<html><body>Not found</body></html>'
    with pytest.raises(ValueError, match="missing __NEXT_DATA__"):
        remember_company_http(99999, http=FakeHttp(html))


def test_remember_company_http_no_company_query_raises() -> None:
    html = _make_next_data([_USER_QUERY])
    with pytest.raises(ValueError, match="no query matching"):
        remember_company_http(99999, http=FakeHttp(html))


def test_remember_company_http_skips_incomplete_stat_entries() -> None:
    incomplete_stats = [
        {"month": "2026-06", "total": 60, "join": 12, "leave": 2},
        {"month": "2026-05", "total": 54},  # missing join/leave
        {"month": "2026-04"},  # missing total/join/leave
    ]
    html = _make_next_data([_USER_QUERY, _company_query(employeeStatistics=incomplete_stats)])
    info = remember_company_http(99999, http=FakeHttp(html))

    assert len(info.employee_stats) == 1
    assert info.employee_stats[0]["month"] == "2026-06"


def test_find_query_by_key_null_state_raises() -> None:
    data = {"props": {"pageProps": {"dehydratedState": {"queries": [
        {"queryKey": ["/companies/123"], "state": None},
    ]}}}}
    with pytest.raises(ValueError, match="non-dict state"):
        find_query_by_key(data, "/companies/")


def test_find_query_by_key_null_data_raises() -> None:
    data = {"props": {"pageProps": {"dehydratedState": {"queries": [
        {"queryKey": ["/companies/123"], "state": {"data": None}},
    ]}}}}
    with pytest.raises(ValueError, match="non-dict data"):
        find_query_by_key(data, "/companies/")


def test_remember_company_http_http_error_propagates() -> None:
    class FailHttp:
        def request_text(self, url: str, **kw: object) -> str:
            raise RuntimeError("HTTP 404 for url")

        def request_json(self, url: str, **kw: object) -> dict:
            raise NotImplementedError

    with pytest.raises(RuntimeError, match="404"):
        remember_company_http(99999, http=FailHttp())


def test_extract_next_data_invalid_json() -> None:
    html = '<script id="__NEXT_DATA__" type="application/json">{invalid</script>'
    with pytest.raises(ValueError, match="invalid __NEXT_DATA__"):
        extract_next_data(html)


def test_find_query_by_key_includes_available_keys_in_error() -> None:
    data = {"props": {"pageProps": {"dehydratedState": {"queries": [_USER_QUERY]}}}}
    with pytest.raises(ValueError, match="/user"):
        find_query_by_key(data, "/companies/")
