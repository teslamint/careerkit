from __future__ import annotations

import json

import pytest

from careerkit.jobs.adapters.platforms.zighang import (
    format_zighang_experience,
    parse_zighang_page,
    supported_origin_url,
    tiptap_to_text,
)
from careerkit.jobs.application.search import parse_experience_range

# Hosts stay separate from the scheme so the publish guard's posting-url rule does not
# read these synthetic ids as pasted postings.
WANTED = "www.wanted.co.kr"
REMEMBER = "career.rememberapp.co.kr"
GROUPBY = "groupby.kr"
SARAMIN = "www.saramin.co.kr"
JOBKOREA = "www.jobkorea.co.kr"
WORK24 = "www.work24.go.kr"
ZIGHANG = "zighang.com"
JOB_ID = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
OTHER_ID = "ffffffff-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


def _text(value: str) -> dict:
    return {"type": "text", "text": value}


def _heading(value: str) -> dict:
    return {"type": "heading", "attrs": {"level": 3}, "content": [_text(value)]}


def _paragraph(value: str) -> dict:
    return {"type": "paragraph", "content": [_text(value)]}


def _bullets(*values: str) -> dict:
    return {"type": "bulletList", "content": [{"type": "listItem", "content": [_paragraph(value)]} for value in values]}


SUMMARY_DOC = {
    "type": "doc",
    "content": [
        _heading("담당업무"), _bullets("예시 서비스 API 개발", "데이터 파이프라인 운영"),
        _heading("필요 역량"), _bullets("백엔드 개발 경험 3년 이상"),
        _heading("우대사항"), _bullets("클라우드 운영 경험"),
        _heading("전형절차"), _paragraph("서류 - 면접 - 최종합격"),
        _heading("보상 및 복리후생"), _bullets("유연 근무"),
    ],
}
IMAGE_ONLY_DOC = {"type": "doc", "content": [{"type": "image", "attrs": {"src": "x"}}, {"type": "paragraph"}]}


def _recruitment(**overrides) -> dict:
    recruitment = {
        "id": JOB_ID,
        "title": "백엔드 개발자",
        "company": {"id": "c-1", "name": "예시회사", "image": None, "hasDetailInfo": False},
        "summary": SUMMARY_DOC,
        "content": IMAGE_ONLY_DOC,
        "redirectUrl": None,
        "careerMin": 3,
        "careerMax": 100,
        "regions": ["서울 강남구"],
        "employeeTypes": ["정규직"],
        "dedup": {"applyOptions": []},
        "status": "OPEN",
    }
    recruitment.update(overrides)
    return recruitment


def _page(payload: str, *, chunks: int = 3) -> str:
    size = max(1, len(payload) // chunks + 1)
    parts = [payload[index:index + size] for index in range(0, len(payload), size)]
    pushes = "".join(f"<script>self.__next_f.push([1,{json.dumps(part)}])</script>" for part in parts)
    return f"<html><head></head><body>{pushes}</body></html>"


def _page_for(recruitment: dict, *, chunks: int = 3) -> str:
    payload = '2:["$","div",null,{"children":"say \\"hi\\""}]\n5:' + json.dumps({"data": {"recruitment": recruitment}}, ensure_ascii=False)
    return _page(payload, chunks=chunks)


class TestParseZighangPage:
    def test_reassembles_chunked_pushes_with_escaped_quotes(self):
        recruitment = _recruitment(title='백엔드 "API" 개발자')
        posting = parse_zighang_page(_page_for(recruitment, chunks=7), JOB_ID)
        assert posting.job_id == JOB_ID
        assert posting.title == '백엔드 "API" 개발자'
        assert posting.company_name == "예시회사"
        assert posting.experience == "3년 이상"
        assert posting.location == "서울 강남구"

    def test_missing_recruitment_raises(self):
        with pytest.raises(ValueError):
            parse_zighang_page(_page('1:["$","div",null,{}]'), JOB_ID)

    def test_invalid_json_raises(self):
        with pytest.raises(ValueError):
            parse_zighang_page(_page('5:{"recruitment":{"id": "' + JOB_ID + '", oops'), JOB_ID)

    def test_invalid_chunk_escape_raises(self):
        with pytest.raises(ValueError):
            parse_zighang_page('<script>self.__next_f.push([1,"bad \\x escape"])</script>', JOB_ID)

    def test_same_id_objects_with_different_content_raise(self):
        payload = json.dumps({"a": {"recruitment": _recruitment()}, "b": {"recruitment": _recruitment(title="다른 포지션")}}, ensure_ascii=False)
        with pytest.raises(ValueError):
            parse_zighang_page(_page(payload), JOB_ID)

    def test_related_cards_and_nested_objects_are_ignored(self):
        card = {"id": OTHER_ID, "title": "관련 공고", "company": {"name": "다른회사"}}
        nested = {"recruitment": _recruitment(title="중첩 포지션", summary=None, content=SUMMARY_DOC)}
        payload = json.dumps({"cards": [{"recruitment": card}], "main": {"recruitment": _recruitment(related=nested)}}, ensure_ascii=False)
        posting = parse_zighang_page(_page(payload), JOB_ID)
        assert (posting.job_id, posting.title, posting.used_summary) == (JOB_ID, "백엔드 개발자", True)

    def test_incomplete_same_id_objects_are_ignored(self):
        partial = {"id": JOB_ID, "title": "백엔드 개발자", "company": {"name": "예시회사"}}
        payload = json.dumps({"a": {"recruitment": partial}, "b": {"recruitment": _recruitment()}}, ensure_ascii=False)
        assert parse_zighang_page(_page(payload), JOB_ID).main_duties == "- 예시 서비스 API 개발\n- 데이터 파이프라인 운영"

    @pytest.mark.parametrize(
        "recruitments",
        [
            [_recruitment(id=OTHER_ID)],
            [{"id": JOB_ID, "title": "백엔드 개발자", "company": {"name": "예시회사"}}],
            [_recruitment(id=OTHER_ID), {"id": JOB_ID, "title": "백엔드 개발자"}],
        ],
    )
    def test_no_complete_object_for_the_expected_id_raises(self, recruitments):
        payload = json.dumps({f"k{index}": {"recruitment": value} for index, value in enumerate(recruitments)}, ensure_ascii=False)
        with pytest.raises(ValueError):
            parse_zighang_page(_page(payload), JOB_ID)

    @pytest.mark.parametrize("expected", [JOB_ID.upper(), "not-a-uuid", f"{JOB_ID}\n"])
    def test_expected_id_must_be_a_lowercase_uuid(self, expected):
        with pytest.raises(ValueError):
            parse_zighang_page(_page_for(_recruitment()), expected)

    def test_repeated_identical_recruitment_objects_are_accepted(self):
        payload = json.dumps({"a": {"recruitment": _recruitment()}, "b": {"recruitment": _recruitment()}}, ensure_ascii=False)
        assert parse_zighang_page(_page(payload), JOB_ID).job_id == JOB_ID

    @pytest.mark.parametrize(
        "overrides",
        [
            {"id": JOB_ID.upper()},
            {"id": "not-a-uuid"},
            {"title": ""},
            {"company": {"name": ""}},
            {"company": None},
            {"careerMin": True},
            {"careerMax": "100"},
            {"regions": [1]},
            {"title": "$undefined"},
            {"careerMax": "$undefined"},
            {"regions": "$undefined"},
            {"regions": ["$1"]},
            {"company": "$5"},
            {"company": {"name": "$undefined"}},
            {"content": "$undefined"},
            {"summary": "$undefined"},
            {"redirectUrl": "$undefined"},
            {"dedup": "$7"},
            {"dedup": {"applyOptions": [{"redirectUrl": "$undefined", "isCurrent": True}]}},
        ],
    )
    def test_malformed_fields_raise(self, overrides):
        with pytest.raises(ValueError):
            parse_zighang_page(_page_for(_recruitment(**overrides)), JOB_ID)

    def test_image_only_content_falls_back_to_summary(self):
        posting = parse_zighang_page(_page_for(_recruitment()), JOB_ID)
        assert posting.used_summary is True
        assert posting.main_duties == "- 예시 서비스 API 개발\n- 데이터 파이프라인 운영"
        assert posting.requirements == "- 백엔드 개발 경험 3년 이상"
        assert posting.preferred == "- 클라우드 운영 경험"
        assert posting.benefits == "- 유연 근무"
        assert posting.introduction == "전형절차\n서류 - 면접 - 최종합격"

    def test_text_content_is_preferred_over_summary(self):
        content = {"type": "doc", "content": [_heading("주요업무"), _bullets("본문 업무"), _heading("자격요건"), _bullets("본문 요건")]}
        posting = parse_zighang_page(_page_for(_recruitment(content=json.dumps(content, ensure_ascii=False))), JOB_ID)
        assert posting.used_summary is False
        assert posting.main_duties == "- 본문 업무"
        assert posting.requirements == "- 본문 요건"

    @pytest.mark.parametrize(
        "overrides",
        [
            {"summary": None},
            {"summary": IMAGE_ONLY_DOC},
            {"content": None, "summary": None},
        ],
    )
    def test_no_text_anywhere_raises(self, overrides):
        with pytest.raises(ValueError):
            parse_zighang_page(_page_for(_recruitment(**overrides)), JOB_ID)

    @pytest.mark.parametrize("content", ["not a document", "[]", "{broken", 7])
    def test_unreadable_content_raises_instead_of_falling_back_to_summary(self, content):
        with pytest.raises(ValueError):
            parse_zighang_page(_page_for(_recruitment(content=content)), JOB_ID)

    def test_pay_and_benefits_heading_maps_to_benefits(self):
        doc = {"type": "doc", "content": [_heading("담당업무"), _bullets("API 개발"), _heading("급여 및 복리후생"), _bullets("식대 지원"), _heading("근무시간"), _paragraph("주 5일")]}
        posting = parse_zighang_page(_page_for(_recruitment(summary=doc)), JOB_ID)
        assert posting.benefits == "- 식대 지원"
        assert posting.introduction == "근무시간\n주 5일"

    def test_apply_options_are_kept(self):
        options = [{"redirectUrl": f"https://{WANTED}/wd/123", "isCurrent": True, "isRepresentative": False}]
        posting = parse_zighang_page(_page_for(_recruitment(dedup={"applyOptions": options})), JOB_ID)
        assert supported_origin_url(posting) == f"https://{WANTED}/wd/123"


class TestTiptapToText:
    def test_nested_bullets_are_indented(self):
        doc = {
            "type": "doc",
            "content": [
                {
                    "type": "bulletList",
                    "content": [
                        {"type": "listItem", "content": [_paragraph("상위"), _bullets("하위")]},
                    ],
                }
            ],
        }
        assert tiptap_to_text(doc) == "- 상위\n  - 하위"

    def test_images_are_ignored(self):
        assert tiptap_to_text(IMAGE_ONLY_DOC) == ""


class TestFormatZighangExperience:
    @pytest.mark.parametrize(
        ("career_min", "career_max", "expected", "parsed"),
        [
            (0, 0, "신입", (None, None)),
            (0, 100, "경력 무관", (None, None)),
            (3, 100, "3년 이상", (3, None)),
            (2, 5, "2~5년", (2, 5)),
            (None, None, "", (None, None)),
            (5, 2, "", (None, None)),
        ],
    )
    def test_table(self, career_min, career_max, expected, parsed):
        assert format_zighang_experience(career_min, career_max) == expected
        assert parse_experience_range(expected) == parsed


class TestSupportedOriginUrl:
    def _posting(self, *, redirect_url=None, options=()):
        return parse_zighang_page(_page_for(_recruitment(redirectUrl=redirect_url, dedup={"applyOptions": list(options)})), JOB_ID)

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (f"https://{WANTED}/wd/123?utm_source=x", f"https://{WANTED}/wd/123"),
            (f"https://{REMEMBER}/job/posting/456", f"https://{REMEMBER}/job/posting/456"),
            (f"https://{GROUPBY}/positions/789", f"https://{GROUPBY}/positions/789"),
            (f"https://{SARAMIN}/zf_user/jobs/relay/view?rec_idx=1011", f"https://{SARAMIN}/zf_user/jobs/relay/view?rec_idx=1011"),
            (f"https://{JOBKOREA}/Recruit/GI_Read/1213", None),
            (f"https://{WORK24}/wk/a/b/1400/empDetailAuthView.do?wantedAuthNo=1", None),
            (f"http://{WANTED}/wd/123", None),
            ("https://unknown.example/wd/123", None),
            (f"https://evil.example/?x={WANTED}/wd/123", None),
            (f"https://{WANTED}.evil.example/wd/123", None),
            (f"https://{WANTED}/wd/123/extra", None),
            (f"https://{SARAMIN}/zf_user/jobs/relay/view?rec_idx=1&rec_idx=2", None),
            (f"https://{SARAMIN}/zf_user/jobs/view?rec_idx=5", f"https://{SARAMIN}/zf_user/jobs/relay/view?rec_idx=5"),
            (f"https://{SARAMIN}/zf_user/company/view?rec_idx=5", None),
            (f"https://{SARAMIN}/?rec_idx=5", None),
            (f"https://{SARAMIN}/zf_user/jobs/relay/view?rec_idx=١٢", None),
            (f"https://{WANTED}/wd/١٢", None),
            (f"https://{REMEMBER}/job/posting/١٢", None),
            (f"https://{GROUPBY}/positions/١٢", None),
            (f"https://{WANTED}:8443/wd/123", None),
            (f"https://{ZIGHANG}/recruitment/{JOB_ID}", None),
        ],
    )
    def test_top_level_redirect(self, raw, expected):
        assert supported_origin_url(self._posting(redirect_url=raw)) == expected

    def test_current_option_wins_over_representative_and_top_level(self):
        posting = self._posting(
            redirect_url=f"https://{GROUPBY}/positions/1",
            options=[
                {"redirectUrl": f"https://{WANTED}/wd/2", "isCurrent": False, "isRepresentative": True},
                {"redirectUrl": f"https://{WANTED}/wd/3", "isCurrent": True, "isRepresentative": False},
            ],
        )
        assert supported_origin_url(posting) == f"https://{WANTED}/wd/3"

    def test_unsupported_current_option_does_not_fall_through(self):
        posting = self._posting(
            redirect_url=f"https://{GROUPBY}/positions/1",
            options=[
                {"redirectUrl": f"https://{JOBKOREA}/Recruit/GI_Read/9", "isCurrent": True, "isRepresentative": False},
                {"redirectUrl": f"https://{WANTED}/wd/2", "isCurrent": False, "isRepresentative": True},
            ],
        )
        assert supported_origin_url(posting) is None

    def test_no_redirect_returns_none(self):
        assert supported_origin_url(self._posting()) is None
