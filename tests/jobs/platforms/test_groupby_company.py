from __future__ import annotations

from careerkit.jobs.adapters.platforms.groupby import groupby_company_from_position


SAMPLE_POSITION_PAYLOAD = {
    "status": 200,
    "data": {
        "id": 12345,
        "name": "시니어 백엔드 개발자",
        "address": "서울 강남구 테헤란로 123",
        "startup": {
            "name": "테스트스타트업",
            "briefIntro": "AI 기반 헬스케어 서비스",
            "memberCount": 30,
            "devCount": 12,
            "fundingRound": "Series A",
            "serviceAreas": [
                {"name": "헬스케어"},
                {"name": "AI"},
            ],
            "location": "서울 서초구",
        },
        "location": {"name": "서울 강남구"},
    },
}


class TestGroupByCompanyFromPosition:
    def test_extracts_all_fields(self):
        info = groupby_company_from_position(SAMPLE_POSITION_PAYLOAD)
        assert info.name == "테스트스타트업"
        assert info.brief_intro == "AI 기반 헬스케어 서비스"
        assert info.member_count == 30
        assert info.dev_count == 12
        assert info.funding_round == "Series A"
        assert info.service_areas == ("헬스케어", "AI")
        assert info.location == "서울 강남구 테헤란로 123"

    def test_prefers_address_over_startup_location(self):
        info = groupby_company_from_position(SAMPLE_POSITION_PAYLOAD)
        assert info.location == "서울 강남구 테헤란로 123"

    def test_falls_back_to_location_object(self):
        payload = {
            "data": {
                "startup": {"name": "테스트"},
                "location": {"name": "판교"},
            },
        }
        info = groupby_company_from_position(payload)
        assert info.location == "판교"

    def test_falls_back_to_startup_location(self):
        payload = {
            "data": {
                "startup": {"name": "테스트", "location": "성수동"},
            },
        }
        info = groupby_company_from_position(payload)
        assert info.location == "성수동"

    def test_missing_fields_produce_none(self):
        payload = {"data": {"startup": {"name": "미니멀"}}}
        info = groupby_company_from_position(payload)
        assert info.name == "미니멀"
        assert info.member_count is None
        assert info.dev_count is None
        assert info.funding_round == ""
        assert info.service_areas == ()
        assert info.location == ""

    def test_flat_payload_without_data_wrapper(self):
        payload = {
            "startup": {
                "name": "플랫",
                "memberCount": 5,
            },
            "address": "역삼동",
        }
        info = groupby_company_from_position(payload)
        assert info.name == "플랫"
        assert info.member_count == 5
        assert info.location == "역삼동"

    def test_string_service_areas(self):
        payload = {
            "data": {
                "startup": {
                    "name": "테스트",
                    "serviceAreas": ["핀테크", "블록체인"],
                },
            },
        }
        info = groupby_company_from_position(payload)
        assert info.service_areas == ("핀테크", "블록체인")

    def test_non_numeric_member_count_returns_none(self):
        payload = {
            "data": {
                "startup": {"name": "테스트", "memberCount": "N/A", "devCount": "미정"},
            },
        }
        info = groupby_company_from_position(payload)
        assert info.member_count is None
        assert info.dev_count is None

    def test_falls_back_to_data_level_member_count(self):
        payload = {
            "data": {
                "startup": {"name": "테스트"},
                "memberCount": 20,
                "devCount": 8,
            },
        }
        info = groupby_company_from_position(payload)
        assert info.member_count == 20
        assert info.dev_count == 8

    def test_empty_startup(self):
        payload = {"data": {"startup": {}}}
        info = groupby_company_from_position(payload)
        assert info.name == ""
        assert info.member_count is None
