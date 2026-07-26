from __future__ import annotations

import json
import re
from typing import Any


def extract_next_data(html: str) -> dict[str, Any]:
    match = re.search(r'<script id="__NEXT_DATA__"[^>]*>([\s\S]*?)</script>', html)
    if not match:
        raise ValueError("missing __NEXT_DATA__ payload")
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ValueError("invalid __NEXT_DATA__ payload") from exc
    if not isinstance(payload, dict):
        raise ValueError("invalid __NEXT_DATA__ payload")
    return payload


def find_query_by_key(data: dict[str, Any], key_substring: str) -> dict[str, Any]:
    queries = data.get("props", {}).get("pageProps", {}).get("dehydratedState", {}).get("queries", [])
    available_keys: list[str] = []
    for query in queries:
        if not isinstance(query, dict):
            continue
        query_key = query.get("queryKey", [])
        key_str = str(query_key)
        available_keys.append(key_str)
        if any(key_substring in str(k) for k in query_key):
            state = query.get("state")
            if not isinstance(state, dict):
                raise ValueError(f"query '{key_str}' has non-dict state")
            outer = state.get("data")
            if not isinstance(outer, dict):
                raise ValueError(f"query '{key_str}' has non-dict data")
            inner = outer.get("data")
            if not isinstance(inner, dict):
                raise ValueError(f"query '{key_str}' has non-dict data.data")
            return inner
    raise ValueError(f"no query matching '{key_substring}' in {available_keys}")
