from __future__ import annotations

from dataclasses import dataclass, replace
from urllib.parse import parse_qs, urlparse

from careerkit.jobs.adapters.platforms.groupby import GroupByAdapter
from careerkit.jobs.adapters.platforms.saramin import SaraminAdapter
from careerkit.jobs.application.config import load_runtime_config
from careerkit.jobs.application.search import SearchService, SearchState, SearchCandidate, PlatformSearchBatch


@dataclass
class StubAdapter:
    platform: str
    batches: list[PlatformSearchBatch]
    received_queries: list[str]

    supports_search: bool = True
    query_independent: bool = False

    def search(self, query: str, *, config, state):
        self.received_queries.append(query)
        return self.batches.pop(0)


@dataclass
class FailingAdapter:
    platform: str
    received_queries: list[str]

    supports_search: bool = True
    query_independent: bool = True

    def search(self, query: str, *, config, state):
        self.received_queries.append(query)
        raise RuntimeError("temporary platform outage")


class RuntimeUnavailableSemanticFilter:
    diagnostic: str | None = None

    def classify(self, title: str) -> str | None:
        self.diagnostic = "semantic filter unavailable: model unavailable offline"
        return None


def _candidate(platform: str, job_id: str, title: str, *, company: str = "Acme", experience: str = "5년 이상") -> SearchCandidate:
    return SearchCandidate(
        platform=platform,
        job_id=job_id,
        raw_id=job_id,
        title=title,
        company=company,
        experience=experience,
        url=f"https://example.com/{platform}/{job_id}",
    )


class RecordingHttpRouter:
    def __init__(self, routes: dict[tuple[str, str, tuple[tuple[str, tuple[str, ...]], ...]], dict]) -> None:
        self.routes = routes
        self.urls: list[str] = []

    def request_json(self, url: str, **kwargs) -> dict:
        del kwargs
        self.urls.append(url)
        return self.routes[self._route_key(url)]

    @staticmethod
    def _route_key(url: str) -> tuple[str, str, tuple[tuple[str, tuple[str, ...]], ...]]:
        parsed = urlparse(url)
        query = tuple(
            sorted((key, tuple(values)) for key, values in parse_qs(parsed.query).items())
        )
        return parsed.netloc, parsed.path, query


def _route_key(url: str) -> tuple[str, str, tuple[tuple[str, tuple[str, ...]], ...]]:
    return RecordingHttpRouter._route_key(url)


def test_search_service_characterizes_common_experience_filtering() -> None:
    config = load_runtime_config(
        {
            "search": {"role": "backend"},
            "platforms": {
                "groupby": {"enabled": True},
                "saramin": {"enabled": True},
                "wanted": {"enabled": True},
            },
            "search_queries": ["백엔드"],
            "execution": {"max_urls_per_run": 10},
            "quick_filters": {"title_include": ["백엔드"], "title_exclude": [], "title_prefer": []},
            "filters": {"min_experience_upper": 9, "max_experience": 10},
            "semantic_filter": {"enabled": False},
        }
    )
    groupby = StubAdapter(
        "groupby",
        [PlatformSearchBatch(candidates=[
            _candidate("groupby", "g-keep", "백엔드 엔지니어", experience="3~10년"),
            _candidate("groupby", "g-drop", "백엔드 플랫폼 엔지니어", experience="1~3년"),
        ])],
        [],
        query_independent=True,
    )
    saramin = StubAdapter(
        "saramin",
        [PlatformSearchBatch(candidates=[
            _candidate("saramin", "s-keep-min", "백엔드 개발자", experience="경력5년↑"),
            _candidate("saramin", "s-keep-none", "백엔드 API 개발자", experience="경력 무관"),
            _candidate("saramin", "s-drop-max", "백엔드 서버 개발자", experience="경력12년↑"),
        ])],
        [],
    )
    wanted = StubAdapter(
        "wanted",
        [PlatformSearchBatch(candidates=[
            _candidate("wanted", "w-empty", "백엔드 엔지니어", experience=""),
        ])],
        [],
    )

    result = SearchService(
        adapters={"groupby": groupby, "saramin": saramin, "wanted": wanted},
        semantic_filter=None,
    ).run(config, SearchState(seen_job_keys=set()))

    assert [item.seen_key for item in result.postings] == [
        "wanted:w-empty",
        "groupby:g-keep",
        "saramin:s-keep-min",
        "saramin:s-keep-none",
    ]
    assert result.total_found == 6
    assert result.filtered_out == 2
    assert result.diagnostics == ()


def test_search_service_runs_groupby_and_saramin_real_adapters_with_runtime_http_router() -> None:
    groupby_url = (
        "https://api.groupby.kr/startup-positions"
        "?isAdvertising=false&limit=10&offset=0&orderBy=-updatedAt"
        "&positionTypes=2&minExperience=5&maxExperience=10"
    )
    saramin_url = (
        "https://m.saramin.co.kr/search/get-recruit-list"
        "?searchword=%EB%B0%B1%EC%97%94%EB%93%9C&searchType=search&page=1"
        "&exp_cd=2&exp_min=5&exp_max=10&exp_none=y"
    )
    router = RecordingHttpRouter(
        {
            _route_key(groupby_url): {
                "status": 200,
                "data": {
                    "items": [
                        {
                            "id": 101,
                            "name": "백엔드 엔지니어",
                            "startup": {"name": "GroupBy Co"},
                            "experienceRange": {"min": 3, "max": 10},
                        }
                    ],
                    "total": 1,
                },
            },
            _route_key(saramin_url): {
                "count": "1",
                "innerHTML": """
                <div id="list_202" class="recruit_container list_link recruit" data-rec_idx=202>
                    <a href="/job-search/view?rec_idx=202" class="link">
                        <div class="list">
                            <p class="tit">백엔드 개발자</p>
                            <div class="meta">
                                <span>서울 강남구</span><span>경력5년↑</span><span>대졸↑</span>
                            </div>
                            <div class="corp">
                                <span class="corp_name">Saramin Co</span>
                            </div>
                        </div>
                    </a>
                </div>
                """,
            },
        }
    )
    config = replace(
        load_runtime_config(
            {
                "search": {"role": "backend"},
                "platforms": {"groupby": {"enabled": True}, "saramin": {"enabled": True}},
                "search_queries": ["백엔드"],
                "execution": {"max_urls_per_run": 10},
                "quick_filters": {"title_include": ["백엔드"], "title_exclude": [], "title_prefer": []},
                "filters": {
                    "min_experience_upper": 9,
                    "max_experience": 10,
                    "api_min_experience": 5,
                    "api_max_experience": 10,
                },
                "semantic_filter": {"enabled": False},
            }
        ),
        http_client=router,
    )

    result = SearchService(
        adapters={"groupby": GroupByAdapter(), "saramin": SaraminAdapter()},
        semantic_filter=None,
    ).run(config, SearchState(seen_job_keys=set()))

    assert [item.seen_key for item in result.postings] == ["groupby:101", "saramin:202"]
    assert [item.experience for item in result.postings] == ["3~10년", "경력5년↑"]
    assert router.urls == [groupby_url, saramin_url]

    groupby_query = parse_qs(urlparse(router.urls[0]).query)
    saramin_query = parse_qs(urlparse(router.urls[1]).query)

    assert groupby_query["minExperience"] == ["5"]
    assert groupby_query["maxExperience"] == ["10"]
    assert saramin_query["exp_cd"] == ["2"]
    assert saramin_query["exp_min"] == ["5"]
    assert saramin_query["exp_max"] == ["10"]
    assert saramin_query["exp_none"] == ["y"]


def test_search_service_normalizes_queries_and_does_not_persist_capped_out_candidates(
    monkeypatch,
) -> None:
    config = load_runtime_config(
        {
            "search": {"role": "backend"},
            "platforms": {"wanted": {"enabled": True}, "remember": {"enabled": True}},
            "search_queries": ["시니어 백엔드", "백엔드 엔지니어", "Senior Backend"],
            "execution": {"max_urls_per_run": 2, "request_delay": 0.25},
            "quick_filters": {"title_include": ["백엔드", "Backend"], "title_exclude": [], "title_prefer": []},
            "semantic_filter": {"enabled": False},
        }
    )
    wanted = StubAdapter("wanted", [PlatformSearchBatch(candidates=[
        _candidate("wanted", "1", "백엔드 엔지니어"),
        _candidate("wanted", "2", "Backend Engineer"),
    ]) for _ in range(2)], [])
    remember = StubAdapter("remember", [PlatformSearchBatch(candidates=[
        _candidate("remember", "9", "Backend Platform Engineer"),
    ]) for _ in range(2)], [])

    service = SearchService(
        adapters={"wanted": wanted, "remember": remember},
        semantic_filter=None,
        existing_record_checker=lambda platform, raw_id: False,
    )

    sleeps: list[float] = []
    monkeypatch.setattr("careerkit.jobs.application.search.time.sleep", sleeps.append)

    result = service.run(config, SearchState(seen_job_keys=set()))

    assert wanted.received_queries == ["백엔드", "Backend"]
    assert remember.received_queries == ["백엔드", "Backend"]
    assert sleeps == [0.25, 0.25, 0.25, 0.25]
    assert [item.job_id for item in result.postings] == ["1", "2"]
    assert result.updated_seen_job_keys == {"wanted:1", "wanted:2"}
    assert "remember:9" not in result.updated_seen_job_keys


def test_search_service_reports_semantic_unavailable_but_keeps_keyword_filtered_results() -> None:
    config = load_runtime_config(
        {
            "search": {"role": "backend"},
            "platforms": {"wanted": {"enabled": True}},
            "search_queries": ["Backend Engineer"],
            "execution": {"max_urls_per_run": 5},
            "quick_filters": {"title_include": ["Engineer"], "title_exclude": [], "title_prefer": []},
            "semantic_filter": {"enabled": True},
        }
    )
    wanted = StubAdapter("wanted", [PlatformSearchBatch(candidates=[
        _candidate("wanted", "1", "Backend Engineer"),
    ])], [])
    service = SearchService(
        adapters={"wanted": wanted},
        semantic_filter=None,
        semantic_capability={"available": False, "reason": "semantic filter unavailable: install careerkit[semantic]"},
        existing_record_checker=lambda platform, raw_id: False,
    )

    result = service.run(config, SearchState(seen_job_keys=set()))

    assert len(result.postings) == 1
    assert result.capabilities["semantic_filter"]["available"] is False
    assert any("careerkit[semantic]" in message for message in result.diagnostics)


def test_search_service_reports_runtime_semantic_failure_and_keeps_results() -> None:
    config = load_runtime_config(
        {
            "search": {"role": "backend"},
            "platforms": {"wanted": {"enabled": True}},
            "search_queries": ["Engineer"],
            "execution": {"max_urls_per_run": 5},
            "quick_filters": {"title_include": ["Engineer"]},
            "semantic_filter": {"enabled": True},
        }
    )
    wanted = StubAdapter(
        "wanted",
        [PlatformSearchBatch(candidates=[_candidate("wanted", "1", "Platform Engineer")])],
        [],
    )

    result = SearchService(
        adapters={"wanted": wanted},
        semantic_filter=RuntimeUnavailableSemanticFilter(),
    ).run(config, SearchState(seen_job_keys=set()))

    assert [item.seen_key for item in result.postings] == ["wanted:1"]
    assert result.capabilities["semantic_filter"] == {
        "available": False,
        "reason": "semantic filter unavailable: model unavailable offline",
    }
    assert result.diagnostics == ("semantic filter unavailable: model unavailable offline",)


def test_search_service_runs_query_independent_adapter_once_and_reports_partial_collection() -> None:
    config = load_runtime_config(
        {
            "search": {"role": "backend"},
            "platforms": {"wanted": {"enabled": True}},
            "search_queries": ["백엔드", "Backend"],
            "execution": {"max_urls_per_run": 5},
            "quick_filters": {"title_include": ["Backend"], "title_exclude": [], "title_prefer": []},
            "semantic_filter": {"enabled": False},
        }
    )
    wanted = StubAdapter(
        "wanted",
        [PlatformSearchBatch(candidates=[_candidate("wanted", "1", "Backend Engineer")], pages_fetched=1000, complete=False)],
        [],
        query_independent=True,
    )

    result = SearchService(adapters={"wanted": wanted}, semantic_filter=None).run(
        config, SearchState(seen_job_keys=set())
    )

    assert wanted.received_queries == ["백엔드"]
    assert result.diagnostics == ("wanted search incomplete after 1000 pages",)


def test_search_service_filters_companies_rejected_in_canonical_records() -> None:
    config = replace(
        load_runtime_config(
            {
                "search": {"role": "backend"},
                "platforms": {"remember": {"enabled": True}},
                "search_queries": ["Backend"],
                "execution": {"max_urls_per_run": 5},
                "quick_filters": {"title_include": ["Backend"]},
                "semantic_filter": {"enabled": False},
            }
        ),
        rejected_companies={"rejected co"},
    )
    remember = StubAdapter(
        "remember",
        [PlatformSearchBatch(candidates=[_candidate("remember", "1", "Backend", company="Rejected Co")])],
        [],
    )

    result = SearchService(adapters={"remember": remember}, semantic_filter=None).run(
        config, SearchState(seen_job_keys=set())
    )

    assert result.postings == ()
    assert result.filtered_out == 1


def test_search_service_continues_after_one_platform_request_fails() -> None:
    config = load_runtime_config(
        {
            "search": {"role": "backend"},
            "platforms": {"wanted": {"enabled": True}, "remember": {"enabled": True}},
            "search_queries": ["Backend"],
            "execution": {"max_urls_per_run": 5},
            "quick_filters": {"title_include": ["Backend"]},
            "semantic_filter": {"enabled": False},
        }
    )
    wanted = FailingAdapter("wanted", [])
    remember = StubAdapter(
        "remember",
        [PlatformSearchBatch(candidates=[_candidate("remember", "2", "Backend Engineer")])],
        [],
    )

    result = SearchService(
        adapters={"wanted": wanted, "remember": remember},
        semantic_filter=None,
    ).run(config, SearchState(seen_job_keys=set()))

    assert [item.seen_key for item in result.postings] == ["remember:2"]
    assert result.diagnostics == ("wanted search failed: temporary platform outage",)


def test_search_service_persists_canonical_duplicate_keys() -> None:
    config = load_runtime_config(
        {
            "search": {"role": "backend"},
            "platforms": {"wanted": {"enabled": True}},
            "search_queries": ["Backend"],
            "execution": {"max_urls_per_run": 5},
            "quick_filters": {"title_include": ["Backend"]},
            "semantic_filter": {"enabled": False},
        }
    )
    wanted = StubAdapter(
        "wanted",
        [PlatformSearchBatch(candidates=[_candidate("wanted", "1", "Backend Engineer")])],
        [],
    )

    result = SearchService(
        adapters={"wanted": wanted},
        semantic_filter=None,
        existing_record_checker=lambda platform, raw_id: True,
    ).run(config, SearchState(seen_job_keys=set()))

    assert result.postings == ()
    assert result.filesystem_duplicates == 1
    assert result.updated_seen_job_keys == {"wanted:1"}
