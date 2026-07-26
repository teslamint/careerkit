"""Canonical company name slugification and normalization.

Consolidates 7 scattered slugify/normalize variants into 2 parameterized
functions used by the packaged jobs application.
"""

import re

_LEGAL_ENTITY_RE = re.compile(
    r'\(주\)|주식회사|\(유\)|유한회사|㈜|\(주\)|Inc\.?|Corp\.?|Co\.,?\s*Ltd\.?',
    re.IGNORECASE,
)

_SLUG_LEGAL_RE = re.compile(
    r"""
    \(주\s?\)
    | 주식회사
    | \(유\s?\)
    | 유한회사
    | ㈜
    | \bInc\.
    | \bCorp\.
    | \bCo\.,?\s*Ltd\.
    """,
    re.VERBOSE | re.IGNORECASE,
)

_JU_PREFIX_RE = re.compile(r"\(주\)|\(주 \)")
_NON_ALNUM_HANGUL_RE = re.compile(r"[^a-zA-Z0-9가-힣]")


def slugify_company(
    name: str,
    *,
    max_len: int = 60,
    fallback: str = "unknown-company",
) -> str:
    """Filesystem-safe company name slug.

    The parameters preserve the two supported product policies: a stable
    default slug and a stricter extraction slug with an empty fallback.
    """
    text = _SLUG_LEGAL_RE.sub("", name or "").strip()
    text = _NON_ALNUM_HANGUL_RE.sub(" ", text).strip()
    result = "-".join(text.lower().split())[:max_len]
    return result or fallback


def normalize_company_name(name: str) -> str:
    """Normalize company name by removing legal entity suffixes.

    Uses the broadest regex (_LEGAL_ENTITY_RE) which handles:
    (주), 주식회사, (유), 유한회사, ㈜, Inc., Corp., Co. Ltd.

    This is the broad normalization policy. Narrow matching policies remain
    local to their application service because they intentionally strip spaces.
    """
    name = _LEGAL_ENTITY_RE.sub('', name)
    return re.sub(r'[\[\]\(\)]', '', name).strip().lower()
