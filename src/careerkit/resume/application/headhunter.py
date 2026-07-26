from __future__ import annotations

from pathlib import Path
import re

from docx import Document

DEFAULT_FONT = "맑은 고딕"

SECTION_PATTERNS = {
    "position_header": r"지원.*회사|Position",
    "personal_name": r"성\s*명",
    "education_header": r"Education|학\s*력\s*사\s*항",
    "career_header": r"Work\s*Experience|경\s*력\s*사\s*항",
    "company_intro": r"□?\s*회사\s*소개",
    "cover_letter_header": r"자기\s*소개서",
}


def analyze_template(docx_path: str) -> dict:
    doc = Document(docx_path)
    detected: dict[str, list[dict]] = {}
    guide_text_candidates: list[dict] = []
    for index, paragraph in enumerate(doc.paragraphs):
        text = paragraph.text.strip()
        if not text:
            continue
        for section_key, pattern in SECTION_PATTERNS.items():
            if re.search(pattern, text, re.IGNORECASE):
                detected.setdefault(section_key, []).append({"idx": index, "text": text})
        if any(keyword in text for keyword in ["★", "ex)", "기본적으로", "본인의 경력", "기억에 남는", "경력기술서는", "분량"]):
            guide_text_candidates.append({"idx": index, "text": text[:60]})
    return {
        "name": Path(docx_path).stem,
        "font": DEFAULT_FONT,
        "detected_sections": {key: [{"paragraph_index": hit["idx"], "text": hit["text"]} for hit in hits] for key, hits in detected.items()},
        "guide_text_to_delete": [candidate["text"] for candidate in guide_text_candidates],
        "template_company_slots": len(detected.get("company_intro", [])),
        "insert_extra_companies_before": "자기소개서",
    }
