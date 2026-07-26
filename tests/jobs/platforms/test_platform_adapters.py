from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from careerkit.jobs.adapters.platforms.groupby import GroupByAdapter, format_groupby_experience
from careerkit.jobs.adapters.platforms.remember import RememberAdapter
from careerkit.jobs.adapters.platforms.wanted import WantedAdapter


class StubHttp:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.urls: list[str] = []

    def request_json(self, url: str, **kwargs) -> dict:
        self.urls.append(url)
        return self.payload


class PartialWantedHttp:
    def __init__(self) -> None:
        self.calls = 0

    def request_json(self, url: str, **kwargs) -> dict:
        self.calls += 1
        if self.calls == 1:
            return {
                "data": [
                    {
                        "id": 1,
                        "position": "Backend",
                        "company": {"name": "Acme"},
                    }
                ],
                "links": {"next": "page-2"},
            }
        raise RuntimeError("page 2 unavailable")


class FailSecondHttp:
    def __init__(self, first_payload: dict) -> None:
        self.first_payload = first_payload
        self.calls = 0

    def request_json(self, url: str, **kwargs) -> dict:
        self.calls += 1
        if self.calls == 1:
            return self.first_payload
        raise RuntimeError("page 2 unavailable")


class SequenceHttp:
    def __init__(self, responses: list[dict | Exception]) -> None:
        self.responses = list(responses)
        self.urls: list[str] = []

    def request_json(self, url: str, **kwargs) -> dict:
        self.urls.append(url)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _config(
    platform: str,
    http,
    *,
    api_min_experience: int | None = 10,
    api_max_experience: int | None = 15,
):
    return SimpleNamespace(
        http_client=http,
        platforms={platform: SimpleNamespace(base_url=f"https://{platform}.example")},
        rate_limits={},
        filters={
            "api_min_experience": api_min_experience,
            "api_max_experience": api_max_experience,
        },
    )


def test_platform_adapters_expose_backend_native_mappings() -> None:
    assert WantedAdapter().native_role_mapping()["job_ids"] == [872]
    assert RememberAdapter().native_role_mapping()["job_category_names"] == [{"level1": "SW개발", "level2": "백엔드"}]
    assert GroupByAdapter().native_role_mapping()["position_types"] == [2]


def test_groupby_search_candidate_uses_raw_id_as_canonical_job_id() -> None:
    candidate = GroupByAdapter()._to_candidate(
        {"id": 123, "name": "Backend", "startup": {"name": "Acme"}},
        "https://groupby.kr",
    )

    assert candidate.job_id == "123"
    assert candidate.raw_id == "123"
    assert candidate.seen_key == "groupby:123"


def test_groupby_search_unwraps_data_envelope_and_formats_experience_range() -> None:
    http = StubHttp(
        {
            "status": 200,
            "data": {
                "items": [
                    {
                        "id": 123,
                        "name": "Backend",
                        "startup": {"name": "Acme"},
                        "experienceRange": {"min": 1, "max": 3},
                    }
                ],
                "total": 1,
            },
        }
    )

    result = GroupByAdapter().search("ignored", config=_config("groupby", http), state=None)

    assert len(result.items) == 1
    assert result.items[0].experience == "1~3년"
    assert result.total_count == 1
    query = parse_qs(urlparse(http.urls[0]).query)
    assert query["positionTypes"] == ["2"]


def test_groupby_search_omits_native_experience_params_without_minimum() -> None:
    http = StubHttp({"status": 200, "data": {"items": [], "total": 0}})

    GroupByAdapter().search(
        "ignored",
        config=_config("groupby", http, api_min_experience=None, api_max_experience=7),
        state=None,
    )

    query = parse_qs(urlparse(http.urls[0]).query)
    assert "minExperience" not in query
    assert "maxExperience" not in query


def test_groupby_search_omits_native_experience_params_without_maximum() -> None:
    http = StubHttp({"status": 200, "data": {"items": [], "total": 0}})

    GroupByAdapter().search(
        "ignored",
        config=_config("groupby", http, api_min_experience=3, api_max_experience=None),
        state=None,
    )

    query = parse_qs(urlparse(http.urls[0]).query)
    assert "minExperience" not in query
    assert "maxExperience" not in query


def test_groupby_search_serializes_configured_experience_range_on_first_and_next_pages() -> None:
    http = SequenceHttp(
        [
            {
                "status": 200,
                "data": {
                    "items": [{"id": 1, "name": "Backend"}],
                    "total": 2,
                },
            },
            {
                "status": 200,
                "data": {
                    "items": [{"id": 2, "name": "Platform"}],
                    "total": 2,
                },
            },
        ]
    )

    GroupByAdapter().search(
        "ignored",
        config=_config("groupby", http, api_min_experience=3, api_max_experience=7),
        state=None,
    )

    first_query = parse_qs(urlparse(http.urls[0]).query)
    second_query = parse_qs(urlparse(http.urls[1]).query)

    assert first_query["positionTypes"] == ["2"]
    assert first_query["orderBy"] == ["-updatedAt"]
    assert first_query["offset"] == ["0"]
    assert first_query["minExperience"] == ["3"]
    assert first_query["maxExperience"] == ["7"]
    assert second_query["positionTypes"] == ["2"]
    assert second_query["orderBy"] == ["-updatedAt"]
    assert second_query["offset"] == ["1"]
    assert second_query["minExperience"] == ["3"]
    assert second_query["maxExperience"] == ["7"]


def test_wanted_search_clamps_experience_years_to_api_ceiling() -> None:
    http = StubHttp({"data": [], "links": {}})

    WantedAdapter().search(
        "ignored",
        config=_config("wanted", http, api_min_experience=15),
        state=None,
    )

    query = parse_qs(urlparse(http.urls[0]).query)
    assert query["years"] == ["10"]


def test_wanted_search_returns_partial_items_after_later_page_failure() -> None:
    http = PartialWantedHttp()

    result = WantedAdapter().search(
        "ignored",
        config=_config("wanted", http),
        state=None,
    )

    assert [item.job_id for item in result.items] == ["1"]
    assert result.complete is False
    assert result.pages_fetched == 1


def test_remember_search_returns_partial_items_after_later_page_failure() -> None:
    http = FailSecondHttp({"data": [{"id": 1, "title": "Backend"}], "meta": {"total_count": 2, "total_pages": 2}})
    result = RememberAdapter().search("Backend", config=_config("remember", http), state=None)
    assert [item.job_id for item in result.items] == ["1"]
    assert result.complete is False


def test_groupby_search_returns_partial_items_after_later_page_failure() -> None:
    http = FailSecondHttp({"status": 200, "data": {"items": [{"id": 1, "name": "Backend"}], "total": 2}})
    result = GroupByAdapter().search("ignored", config=_config("groupby", http), state=None)
    assert [item.job_id for item in result.items] == ["1"]
    assert result.complete is False


def test_remember_candidate_uses_listing_experience_bounds() -> None:
    candidate = RememberAdapter()._to_candidate(
        {
            "id": 456,
            "title": "Backend",
            "organization": {"name": "Acme"},
            "min_experience": 3,
            "max_experience": 7,
        },
        "https://remember.example",
    )

    assert candidate.experience == "3~7년"


def test_remember_candidate_treats_zero_bounds_as_unrestricted() -> None:
    candidate = RememberAdapter()._to_candidate(
        {
            "id": 456,
            "title": "Backend",
            "organization": {"name": "Acme"},
            "min_experience": 0,
            "max_experience": 0,
        },
        "https://remember.example",
    )

    assert candidate.experience == "경력 무관"


def test_groupby_max_only_experience_is_parseable_by_shared_filter() -> None:
    assert format_groupby_experience(
        {"careerType": "경력", "experienceRange": {"min": None, "max": 3}}
    ) == "3년 이하"
