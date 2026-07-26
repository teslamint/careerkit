from __future__ import annotations


def filter_content(content: str, variant: str) -> str:
    lines = content.split("\n")
    result: list[str] = []
    current_block: str | None = None
    block_start_line: int | None = None
    include_map = {
        "job": {"job-only", "common"},
        "public": {"public-only", "common"},
    }
    include_tags = include_map.get(variant, {"common"})

    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        start_tags = {
            "<!-- job-only:start -->": "job-only",
            "<!-- public-only:start -->": "public-only",
            "<!-- common:start -->": "common",
        }
        end_tags = {
            "<!-- job-only:end -->": "job-only",
            "<!-- public-only:end -->": "public-only",
            "<!-- common:end -->": "common",
        }
        if stripped in start_tags:
            if current_block is not None:
                raise ValueError(f"Line {i}: nested tag '{stripped}' inside '{current_block}' block")
            current_block = start_tags[stripped]
            block_start_line = i
            continue
        if stripped in end_tags:
            expected = end_tags[stripped]
            if current_block != expected:
                raise ValueError(f"Line {i}: '{stripped}' without matching start tag")
            current_block = None
            block_start_line = None
            continue
        if current_block is None or current_block in include_tags:
            result.append(line)

    if current_block is not None:
        raise ValueError(f"Unclosed '{current_block}' block starting at line {block_start_line}")
    return "\n".join(result)
