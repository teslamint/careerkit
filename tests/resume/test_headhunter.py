from __future__ import annotations

from docx import Document

from careerkit.resume.application.headhunter import analyze_template


def test_analyze_template_detects_sections(tmp_path) -> None:
    path = tmp_path / "template.docx"
    doc = Document()
    for text in ["지원 회사", "성명", "Education", "Work Experience", "회사 소개", "자기소개서", "★ guide"]:
        doc.add_paragraph(text)
    doc.save(path)

    result = analyze_template(str(path))

    assert result["name"] == "template"
    assert result["font"] == "맑은 고딕"
    assert result["template_company_slots"] == 1
    assert result["insert_extra_companies_before"] == "자기소개서"
    assert "guide" in result["guide_text_to_delete"][0]
