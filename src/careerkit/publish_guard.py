"""Deterministic gate that keeps corpus-derived identifiers out of the public repository.

Three layers, all blocking: structural regex rules, morphological proper-noun tagging
minus the merge-base vocabulary, and a live term list derived from the private record
store. See docs/specs in the workspace repository for the approved design.

Exit status: ``0`` clean, ``1`` findings, ``2`` usage or resolution error.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import html
import importlib
import json
import os
import re
import subprocess
import sys
import unicodedata
from collections.abc import Iterator
from typing import Any
from pathlib import Path
from urllib.parse import unquote

__all__ = ["Finding", "RULE_NAMES", "Terms", "scan_text", "generate_terms", "main"]

RULE_NAMES: frozenset[str] = frozenset(
    {
        "private-path",
        "record-key",
        "posting-url",
        "email",
        "legal-suffix",
        "entity-suffix",
        "entity-position",
        "quoted-corpus-prose",
        "proper-noun",
        "local-term",
        "term-file",
    }
)

_TITLE_NOISE = re.compile(
    r"(engineer|developer|backend|frontend|server|엔지니어|개발자|백엔드|프론트|서버)",
    re.IGNORECASE,
)
_VALUE_SPLIT = re.compile(r"\s*[-/|,·]\s*|\s*[\(\)\[\]]\s*|\s+")
_R2_BOUND = r"(?:^|[\s`\"'\(\)\[\]·,/|=:]|\(주\))"
_R2_TRAIL = (
    r"(?:$|[\s`\"'\(\)\[\]·,/|=:]"
    r"|은|는|이|가|을|를|에|의|에서|으로|와|과|도|만|사|팀|측|라는|입니다|이다)"
)
# The guard's own definition files carry the rule patterns as source text; scanning
# them would let the guard block its own implementation. The exemption is exact-path,
# never a prefix or suffix match, and messages stay scanned everywhere.
_OWN_DEFINITION_PATHS = frozenset(
    {
        "src/careerkit/publish_guard.py",
        "tests/contract/test_publish_deidentification.py",
        "tests/contract/test_publish_guard_mutation.py",
    }
)


def _is_own_definition(path_name: str) -> bool:
    return path_name in _OWN_DEFINITION_PATHS or path_name.replace("\\", "/") in {
        name.replace("src/", "") for name in _OWN_DEFINITION_PATHS
    }


_TEXT_SUFFIXES = {
    ".md", ".py", ".txt", ".json", ".yaml", ".yml", ".toml", ".cjs", ".js", ".ts",
    ".html", ".css", ".sh", ".cfg", ".ini", ".lock", ".example", ".j2",
}
_BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".whl", ".tar", ".gz",
    ".svg", ".ico", ".woff", ".woff2", ".ttf", ".bin", ".ppm", ".class",
}


@dataclasses.dataclass(frozen=True)
class Finding:
    """One blocking observation. ``evidence`` is the matched fragment."""

    rule: str
    evidence: str
    line: int
    path: str = ""


@dataclasses.dataclass(frozen=True)
class Terms:
    """Layer 3's two disjoint buckets."""

    substring: frozenset[str]
    bounded: frozenset[str]


# --- Normalization (spec revision 5) ---------------------------------------


def _decode_to_fixed_point(text: str) -> str:
    decoded = text
    while True:
        next_decoded = html.unescape(unquote(decoded))
        if next_decoded == decoded:
            return decoded
        decoded = next_decoded


def normalize_text(text: str) -> str:
    """Recursive URL-decode to fixed point, backslash fold, NFKC, strip category Cf."""
    decoded = _decode_to_fixed_point(text).replace("\\", "/")
    normalized = unicodedata.normalize("NFKC", decoded)
    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Cf")


# --- Platform set (A-11: re-exported, never hand-kept) -----------------------


def platform_names() -> frozenset[str]:
    from careerkit.jobs.application.storage_migration import _KNOWN_PLATFORMS

    return frozenset(_KNOWN_PLATFORMS)


# --- Layer 1: structural rules ----------------------------------------------

_PRIVATE_NARROW = re.compile(
    r"(?:~|/Users/|/home/)[^\s]*private(?:/|\b)|private/jd/records/[a-z]+/[^\s/]+",
    re.IGNORECASE,
)
_RECORD_KEY = re.compile(
    r"(?<![\w/-])(?P<platform>[a-z][a-z0-9]*)/(?P<key>[0-9]{2,}|[A-Za-z0-9-]{20,})"
    r"(?![\w/-])"
)
_POSTING_URL = re.compile(
    r"https?://[^\s]*?(?:wanted\.co\.kr|saramin\.co\.kr|jobkorea\.co\.kr|jumpit\.co\.kr"
    r"|rememberapp\.co\.kr|groupby\.co\.kr|offercent\.kr|greeting\.hr)[^\s]*",
    re.IGNORECASE,
)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_EMAIL_EXEMPT = re.compile(r"^(?:noreply@|[^@]*@example\.)", re.IGNORECASE)
_LEGAL_SUFFIX = re.compile(
    r"(?:주식회사|㈜|\(주\)|유한회사|Ltd\.?|Inc\.?|LLC)(?=\s|[^\w가-힣]|$)",
    re.IGNORECASE,
)
_ENTITY_SUFFIX = re.compile(
    r"[가-힣A-Za-z0-9]{1,20}\s*(?:그룹|홀딩스|인더스트리|테크|랩스|웍스|컴퍼니)"
)
_EMPLOYMENT = re.compile(r"합류|입사|이직|재직|영입|스카웃|계약")
_ENTITY_POSITION = re.compile(r"(?:합류|스카웃|영입|이직|재직)\s*(?:했|하|해)")
_QUOTED = re.compile(
    r"[`\"'\u201c\u2018]([^`\"'\u201d\u2019]+)[`\"'\u201d\u2019]"
)
_KOREAN_ENDINGS = re.compile(
    r"(합류|입사|재직|합니다|입니다|싶어요|좋아요|주세요|드립니다|였습니다|했습니다)"
)
_QUESTION_ENDINGS = re.compile(r"(하나요|가요|까요|나요|슴니까)")
# Generic occupational nouns chosen from dictionary vocabulary, not the corpus (S-10).
_GENERIC_SUBJECTS = frozenset(
    {
        "개발자", "엔지니어", "디자이너", "매니저", "기획자", "회사", "팀",
        "동료", "사람", "사용자", "고객", "개발", "서비스", "프로덕트", "조직",
        "예시", "샘플", "테스트", "더미",
    }
)
_TERM_FILE = re.compile(r"publish-guard-terms\.json")


def _layer1(text: str, line: int) -> Iterator[Finding]:
    private_spans: set[tuple[int, int]] = set()
    for match in _PRIVATE_NARROW.finditer(text):
        yield Finding("private-path", match.group(0), line)
        private_spans.add(match.span())
    for match in re.finditer(r"(?:~|/Users/|/home/)[^\s]*", text):
        if "private" in match.group(0) and not any(
            start <= match.start() < end for start, end in private_spans
        ):
            yield Finding("private-path", match.group(0), line)
    # The repository's canonical key label is platform:job_id (career-jobs cli output);
    # the slash and space forms appear when the same key is pasted from other tools.
    for match in re.finditer(
        r"(?<![\w/-])(?P<platform>[a-z][a-z0-9]*)[:/ ](?P<key>[0-9]{2,}|[A-Za-z0-9-]{20,})(?![\w/-])",
        text,
    ):
        if match.group("platform") in platform_names():
            yield Finding("record-key", match.group(0), line)
    for match in _POSTING_URL.finditer(text):
        yield Finding("posting-url", match.group(0), line)
    for match in _EMAIL.finditer(text):
        if not _EMAIL_EXEMPT.match(match.group(0)):
            yield Finding("email", match.group(0), line)
    for match in re.finditer(
        r"(?:주식회사|㈜|\(주\)|유한회사|Ltd\.?|Inc\.?|LLC)(?=\s|[^\w가-힣]|$)",
        text,
        re.IGNORECASE,
    ):
        yield Finding("legal-suffix", match.group(0), line)
    for match in re.finditer(
        r"(?P<head>[가-힣]{2,19}|[A-Za-z0-9]{2,19})(?P<word>그룹|홀딩스|인더스트리|테크|랩스|웍스|컴퍼니)",
        text,
    ):
        head = match.group("head")
        if not head or head in _GENERIC_SUBJECTS:
            continue
        before = text[max(0, match.start() - 4) : match.start()]
        if re.search(r"(?:주식회사|㈜|\(주\)|Ltd\.?|Inc\.)\s*$", before):
            continue
        yield Finding("entity-suffix", match.group(0), line)
    for match in _ENTITY_POSITION.finditer(text):
        prefix = text[max(0, match.start() - 24) : match.start()]
        suffix = text[match.end() : match.end() + 5]
        if _QUESTION_ENDINGS.search(suffix):
            continue
        if not any(
            re.search(rf"(?<![\w가-힣]){re.escape(word)}", prefix)
            for word in _GENERIC_SUBJECTS
        ):
            yield Finding("entity-position", match.group(0), line)
    for match in _QUOTED.finditer(text):
        fragment = match.group(1)
        if (
            len(fragment) >= 12
            and re.search(r"[가-힣]", fragment)
            and _KOREAN_ENDINGS.search(fragment)
            and not _QUESTION_ENDINGS.search(fragment)
            # Quoted Korean prose is ordinary documentation; require an employment
            # carrier inside the fragment so README quotes and UI labels stay quiet.
            and _EMPLOYMENT.search(fragment)
        ):
            yield Finding("quoted-corpus-prose", match.group(0), line)
    for match in _TERM_FILE.finditer(text):
        yield Finding("term-file", match.group(0), line)




def _import_analyzer() -> Any | None:
    """Import and construct the analyzer without a resolvable import edge.

    Returns an instance so no caller can pass the class where a tokenizer is expected;
    pyright basic mode cannot resolve the class through importlib anyway.
    """
    try:
        module = importlib.import_module("kiwipiepy")
    except ImportError:
        return None
    return getattr(module, "Kiwi")()


# --- Layer 2: morphological --------------------------------------------------


def _merge_base_ref(git_cwd: Path | None = None) -> str:
    def git(*command: str) -> str:
        result = subprocess.run(
            ["git", *command], cwd=git_cwd, capture_output=True, check=False
        )
        return result.stdout.decode("utf-8", errors="replace").strip()

    upstream = git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{push}")
    if upstream and "origin/" in upstream:
        base = git("merge-base", "HEAD", upstream)
        if base:
            return base
    origin_head = git("rev-parse", "--verify", "origin/main")
    if origin_head:
        base = git("merge-base", "HEAD", origin_head)
        if base:
            return base
    return ""


def vocabulary_cache_path(repo: Path | None = None) -> Path | None:
    cwd = repo or Path.cwd()
    common = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=cwd,
        capture_output=True,
        check=False,
    ).stdout.decode("utf-8", errors="replace").strip()
    if not common:
        return None
    return Path(common) / "publish-guard-vocabulary"


def _vocab_cache_key(tree: str) -> str:
    """Cache key folds the build algorithm version and the analyzer version."""
    try:
        analyzer_version = importlib.import_module("kiwipiepy").__version__
    except (ImportError, AttributeError):
        analyzer_version = "none"
    return f"v2:{analyzer_version}:{tree}"


def build_vocabulary(base: str, repo: Path | None = None) -> frozenset[str]:
    """NNP forms of the merge-base tree object plus that base's commit messages.

    ``repo`` scopes every git call and the cache to the checkout under scan; the
    default (None) uses the process CWD, which is how the hooks run.
    """
    cwd = repo or Path.cwd()
    if not base:
        raise SystemExit("layer 2 base ref does not resolve; refusing an empty vocabulary")
    cache = vocabulary_cache_path(cwd)
    tree = subprocess.run(
        ["git", "rev-parse", f"{base}^{{tree}}"],
        cwd=cwd,
        capture_output=True,
        check=False,
    ).stdout.decode("utf-8", errors="replace").strip()
    if cache and cache.is_file():
        try:
            cached = json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cached = None
        if cached and cached.get("key") == _vocab_cache_key(tree):
            return frozenset(cached["forms"])
    kiwi = _import_analyzer()
    if kiwi is None:
        return frozenset()
    forms: set[str] = set()
    for entry in subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", base],
        cwd=cwd,
        capture_output=True,
        check=False,
    ).stdout.decode("utf-8", errors="replace").splitlines():
        rel = entry.strip()
        if not rel or Path(rel).suffix.lower() not in _TEXT_SUFFIXES:
            continue
        blob = subprocess.run(
            ["git", "show", f"{base}:{rel}"],
            cwd=cwd,
            capture_output=True,
            check=False,
        ).stdout
        try:
            blob_text = blob.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if blob_text and re.search(r"[가-힣]", blob_text):
            # Per line, like the scanner: Kiwi's output is context-sensitive, so a
            # whole-blob pass misses proper-noun forms the per-line scan then flags.
            for blob_line in blob_text.splitlines():
                if blob_line.strip():
                    forms |= _nnp_forms(kiwi, normalize_text(blob_line))
    messages = subprocess.run(
        ["git", "log", "-200", "--format=%B", base],
        cwd=cwd,
        capture_output=True,
        check=False,
    ).stdout.decode("utf-8", errors="replace")
    if messages:
        for message_line in messages.splitlines():
            if message_line.strip():
                forms |= _nnp_forms(kiwi, normalize_text(message_line))
    if cache:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(
            json.dumps(
                {"key": _vocab_cache_key(tree), "forms": sorted(forms)}
            ), encoding="utf-8"
        )
    return frozenset(forms)


def _layer2(
    text: str,
    analyzer: Any,
    vocabulary: frozenset[str],
    line: int,
) -> Iterator[Finding]:
    for token in analyzer.tokenize(text):
        form = getattr(token, "form")
        tag = getattr(token, "tag")
        if tag == "NNP" and form not in vocabulary:
            yield Finding("proper-noun", str(form), line)




def _nnp_forms(kiwi: Any, text: str) -> frozenset[str]:
    """NNP forms from a tokenization pass, typed loosely for pyright's benefit."""
    forms: set[str] = set()
    for token in kiwi.tokenize(text):
        if str(getattr(token, "tag", "")) == "NNP":
            forms.add(str(getattr(token, "form", "")))
    return frozenset(forms)


# --- Layer 3: live term list -------------------------------------------------


def generate_terms(workspace: str | Path) -> Terms:
    """Derive the R1/R2 term list from the workspace record store (rev 6 contract)."""
    root = Path(workspace).expanduser() / "private" / "jd" / "records"
    if not root.is_dir():
        print(f"record store not found under {root}", file=sys.stderr)
        raise SystemExit(2)
    values: list[str] = []
    for record in root.glob("*/*/record.json"):
        try:
            payload = json.loads(record.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        raw = payload.get("record", {}).get("company")
        if raw:
            name = re.sub(r"\(주\)|주식회사|\([^)]*\)", "", str(raw)).strip()
            if name:
                values.append(name)
    if not values:
        print("record store is empty; refusing to cache an empty list", file=sys.stderr)
        raise SystemExit(2)
    exclusions = _load_generic_exclusions(Path(workspace).expanduser())
    return _build_terms(values, exclusions)


def _build_terms(
    values: list[str], exclusions: frozenset[str] = frozenset()
) -> Terms:
    substring: set[str] = set()
    bounded: set[str] = set()
    for value in values:
        if _TITLE_NOISE.search(value) and len(value) >= 4 and re.search(r"[가-힣]", value):
            for piece in (x.strip() for x in _VALUE_SPLIT.split(value)):
                if (
                    piece
                    and len(piece) >= 4
                    and not _TITLE_NOISE.search(piece)
                    and re.search(r"[가-힣]", piece)
                    and piece not in exclusions
                ):
                    substring.add(piece)
        elif len(value) >= 4 and re.search(r"[가-힣]", value):
            if value not in exclusions:
                substring.add(value)
        elif 2 <= len(value) <= 3 and value not in exclusions:
            bounded.add(value)
    return Terms(frozenset(substring), frozenset(bounded))


def _load_generic_exclusions(workspace: Path) -> frozenset[str]:
    """Bounded-term exclusions live in the untracked workspace cache, never tracked.

    A tracked exclusion list that names store values is itself a corpus-derived
    denylist (A-13); the workspace cache is where corpus-derived vocabulary belongs.
    The file carries plain JSON: {"generic_exclusions": [...]}.
    """
    path = workspace / "private" / "jd" / "derived" / "publish-guard-terms.json"
    if not path.is_file():
        return frozenset()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return frozenset()
    items = payload.get("generic_exclusions") or []
    return frozenset(str(item) for item in items)


def terms_cache_key(workspace: str | Path) -> str:
    """Digest of sorted company values (spec revision 6)."""
    root = Path(workspace).expanduser() / "private" / "jd" / "records"
    values: list[str] = []
    for record in root.glob("*/*/record.json"):
        try:
            payload = json.loads(record.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        raw = payload.get("record", {}).get("company")
        if raw:
            name = re.sub(r"\(주\)|주식회사|\([^)]*\)", "", str(raw)).strip()
            if name:
                values.append(name)
    return hashlib.sha256("\x00".join(sorted(values)).encode("utf-8")).hexdigest()[:16]


def load_terms(workspace: str | Path) -> tuple[Terms, str]:
    """Read or regenerate the untracked cache; exit 2 when the store is empty."""
    ws = Path(workspace).expanduser()
    cache = ws / "private" / "jd" / "derived" / "publish-guard-terms.json"
    key = terms_cache_key(ws)
    if cache.is_file():
        try:
            payload = json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = None
        if payload and payload.get("key") == key:
            exclusions = _load_generic_exclusions(ws)
            terms = Terms(
                frozenset(payload.get("substring") or []) - exclusions,
                frozenset(payload.get("bounded") or []) - exclusions,
            )
            if terms.substring or terms.bounded:
                return terms, key
    terms = generate_terms(ws)
    cache.parent.mkdir(parents=True, exist_ok=True)
    exclusions = _load_generic_exclusions(ws)
    terms = Terms(
        terms.substring - exclusions,
        terms.bounded - exclusions,
    )
    cache.write_text(
        json.dumps(
            {
                "key": key,
                "substring": sorted(terms.substring),
                "bounded": sorted(terms.bounded),
                "generic_exclusions": sorted(exclusions),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return terms, key


def _layer3(text: str, terms: Terms | None, line: int) -> Iterator[Finding]:
    if terms is None:
        return
    for term in terms.substring:
        if term in text:
            yield Finding("local-term", term, line)
    for term in terms.bounded:
        if re.search(_R2_BOUND + re.escape(term) + _R2_TRAIL, text):
            yield Finding("local-term", term, line)


# --- Public scan API --------------------------------------------------------


# --- Public scan API# --- Public scan API --------------------------------------------------------


def scan_text(
    text: str,
    *,
    terms: Terms | None = None,
    layers: frozenset[int] = frozenset({1, 2, 3}),
    analyzer: object = None,
    vocabulary: frozenset[str] = frozenset(),
    base: int = 1,
) -> Iterator[Finding]:
    """Run the active layers over one block of text.

    ``terms=None`` disables layer 3 for this call; a caller that has no workspace must
    pass ``--layers 1,2`` at the CLI instead of expecting a silent skip.
    """
    normalized = normalize_text(text)
    if 2 in layers and analyzer is None:
        layers = layers - {2}
    for offset, block in enumerate(normalized.splitlines() or [normalized]):
        line = base + offset
        if 1 in layers:
            yield from _layer1(block, line)
        if 3 in layers:
            yield from _layer3(block, terms, line)
        # Per line, like the other layers: a whole-block pass attributes every
        # proper-noun to the block's base line, so a multi-line message cannot be
        # located from the finding.
        if 2 in layers and analyzer is not None:
            yield from _layer2(block, analyzer, vocabulary, line)


# --- CLI --------------------------------------------------------------------

def _git_output(*command: str) -> str:
    return subprocess.run(
        ["git", *command], capture_output=True, check=False
    ).stdout.decode("utf-8", errors="replace")


def _unquote_git_path(path: str) -> str:
    """Decode git's C-style path quoting when core.quotePath is on."""
    if not (path.startswith('"') and path.endswith('"')):
        return path
    body = path[1:-1].encode("utf-8")
    pieces: list[bytes] = []
    i = 0
    while i < len(body):
        if body[i : i + 1] == b"\\" and i + 1 < len(body):
            token = body[i + 1 : i + 2]
            if token in b"abtnvfr" or token.isdigit():
                if token.isdigit():
                    octal_digits = body[i + 1 : i + 4]
                    pieces.append(bytes([int(octal_digits, 8)]))
                    i += 4
                    continue
                pieces.append({b"n": b"\n", b"t": b"\t", b"r": b"\r", b"a": b"\a", b"b": b"\b", b"v": b"\v"}.get(token, token))
                i += 2
                continue
        pieces.append(body[i : i + 1])
        i += 1
    return b"".join(pieces).decode("utf-8", errors="replace")


def _parse_added(diff: str) -> list[tuple[str, int, str]]:
    lines: list[tuple[str, int, str]] = []
    current: str | None = None
    line_no = 0
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            quoted = _unquote_git_path(raw[4:])
            current = quoted[2:] if quoted.startswith("b/") else quoted
        if raw.startswith("@@"):
            match = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", raw)
            line_no = int(match.group(1)) if match else 0
            continue
        if raw.startswith("+") and not raw.startswith("+++") and current:
            lines.append((current, line_no, raw[1:]))
            line_no += 1
    return lines


def _added_commit_paths(*shas: str) -> list[str]:
    paths: list[str] = []
    for sha in shas:
        names = _git_output("show", sha, "--name-status", "--format=")
        for row in names.splitlines():
            parts = row.split("\t")
            if len(parts) >= 2 and parts[0] in {"A", "C", "R"}:
                paths.append(parts[-1])
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m careerkit.publish_guard")
    parser.add_argument("--message", metavar="PATH")
    parser.add_argument("--staged", action="store_true")
    parser.add_argument("--commits", metavar="BASE..HEAD")
    parser.add_argument("--exclude-remote", metavar="NAME", help="commits on this remote's tracking refs are already published")
    parser.add_argument("--generate-terms", metavar="WORKSPACE")
    parser.add_argument("--layers", default="1,2,3")
    parser.add_argument("--no-evidence", action="store_true")
    args = parser.parse_args(argv)

    try:
        layers = frozenset(int(x) for x in args.layers.split(",") if x.strip())
    except ValueError:
        print("--layers accepts comma-separated integers from {1,2,3}", file=sys.stderr)
        return 2
    if not layers or not layers <= {1, 2, 3}:
        print("--layers accepts a subset of 1,2,3", file=sys.stderr)
        return 2

    if args.generate_terms:
        terms, key = load_terms(args.generate_terms)
        print(
            f"terms: {len(terms.substring)} substring + {len(terms.bounded)} bounded; "
            f"key {key}"
        )
        return 0

    workspace = os.environ.get("CAREER_WORKSPACE", "")
    terms: Terms | None = None
    if 3 in layers:
        if not workspace:
            print(
                "CAREER_WORKSPACE is not set; layer 3 cannot run. Set it or pass --layers.",
                file=sys.stderr,
            )
            return 2
        terms, _ = load_terms(workspace)

    analyzer = None
    vocabulary: frozenset[str] = frozenset()
    if 2 in layers:
        analyzer = _import_analyzer()
        if analyzer is None:
            print(
                "analyzer inactive: kiwipiepy is not installed; layer 2 skipped",
                file=sys.stderr,
            )
        else:
            base = _merge_base_ref(Path.cwd())
            if not base:
                print("layer 2 base ref does not resolve; layer 2 skipped", file=sys.stderr)
                # An unresolvable base with a live analyzer would run layer 2 with no
                # subtraction and flag every proper noun; skip layer 2 outright.
                analyzer = None
            else:
                vocabulary = build_vocabulary(base)

    def _scan_path_name(path_name: str) -> list[Finding]:
        return [
            dataclasses.replace(finding, path=path_name)
            for finding in scan_text(
                normalize_text(path_name), terms=terms, layers=layers,
                analyzer=analyzer, vocabulary=vocabulary, base=0
            )
        ]

    findings: list[Finding] = []
    if args.message:
        path = Path(args.message)
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            print(f"cannot read {path}: {error}", file=sys.stderr)
            return 2
        findings = list(
            scan_text(content, terms=terms, layers=layers, analyzer=analyzer, vocabulary=vocabulary)
        )
    elif args.staged:
        diff = _git_output(
            "diff", "--cached", "--no-ext-diff", "--no-color", "--no-textconv", "-U0"
        )
        added = _parse_added(diff)
        for path_name, line_no, content in added:
            if _is_own_definition(path_name):
                continue
            for finding in scan_text(content, terms=terms, layers=layers, analyzer=analyzer, vocabulary=vocabulary, base=line_no):
                findings.append(dataclasses.replace(finding, path=path_name))
        names = _git_output("diff", "--cached", "--name-status")
        binary_count = 0
        for row in names.splitlines():
            status, _, path_name = row.partition("\t")
            if status == "D":
                continue
            if Path(path_name).suffix.lower() in _BINARY_SUFFIXES:
                binary_count += 1
                continue
            findings.extend(_scan_path_name(path_name))
        if binary_count:
            if args.no_evidence:
                print(f"unscannable binary paths: {binary_count}", file=sys.stderr)
            else:
                print(
                    f"unscannable binary paths: {binary_count} (pass --no-evidence to see a count only)",
                    file=sys.stderr,
                )
    elif args.commits:
        if ".." not in args.commits and not args.exclude_remote:
            print("--commits expects BASE..HEAD, or an oid with --exclude-remote", file=sys.stderr)
            return 2
        if args.exclude_remote and ".." not in args.commits:
            # Fallback for a first push: scan exactly the commits the named remote's
            # tracking refs do not already carry, per commit so a diverged set works.
            rev_list = subprocess.run(
                ["git", "rev-list", args.commits, "--not", f"--remotes={args.exclude_remote}"],
                capture_output=True,
                check=False,
            )
            if rev_list.returncode != 0:
                print(
                    f"cannot resolve {args.commits} against --remotes={args.exclude_remote}",
                    file=sys.stderr,
                )
                return 2
            new_shas = rev_list.stdout.decode("utf-8", errors="replace").split()
            if not new_shas:
                return 0
            messages = "\n".join(
                _git_output("log", "-1", "--format=%B", sha) for sha in new_shas
            )
            findings.extend(
                scan_text(messages, terms=terms, layers=layers, analyzer=analyzer, vocabulary=vocabulary)
            )
            for sha in new_shas:
                added = _parse_added(_git_output("show", "-U0", "--format=", sha))
                for path_name, line_no, content in added:
                    if _is_own_definition(path_name):
                        continue
                    for finding in scan_text(content, terms=terms, layers=layers, analyzer=analyzer, vocabulary=vocabulary, base=line_no):
                        findings.append(dataclasses.replace(finding, path=path_name))
            for path_name in _added_commit_paths(sha):
                findings.extend(_scan_path_name(path_name))
            return _report(findings, args)
        base, _, head = args.commits.partition("..")
        resolution = subprocess.run(
            ["git", "rev-parse", "--verify", base], capture_output=True, check=False
        )
        head_resolution = subprocess.run(
            ["git", "rev-parse", "--verify", head], capture_output=True, check=False
        )
        if resolution.returncode != 0 or head_resolution.returncode != 0:
            print(f"range {base}..{head} does not resolve", file=sys.stderr)
            return 2
        messages = _git_output("log", f"{base}..{head}", "--format=%B")
        findings.extend(
            scan_text(messages, terms=terms, layers=layers, analyzer=analyzer, vocabulary=vocabulary)
        )
        # Per commit, like the fallback: a line added in one commit and removed in a
        # later commit of the same push still reaches public history in that commit.
        for sha in _git_output("rev-list", f"{base}..{head}").split():
            added = _parse_added(_git_output("show", "-U0", "--format=", sha))
            for path_name, line_no, content in added:
                if _is_own_definition(path_name):
                    continue
                for finding in scan_text(content, terms=terms, layers=layers, analyzer=analyzer, vocabulary=vocabulary, base=line_no):
                    findings.append(dataclasses.replace(finding, path=path_name))
            for path_name in _added_commit_paths(sha):
                findings.extend(_scan_path_name(path_name))
    else:
        parser.print_usage(sys.stderr)
        return 2

    return _report(findings, args)


def _report(findings: list[Finding], args: Any) -> int:
    if not findings:
        return 0
    for finding in findings:
        where = f"{finding.path}:{finding.line}" if finding.path else f"line {finding.line}"
        if args.no_evidence:
            print(f"{where}: [{finding.rule}]", file=sys.stderr)
        else:
            print(f"{where}: [{finding.rule}] {finding.evidence}", file=sys.stderr)
    return 1



if __name__ == "__main__":
    raise SystemExit(main())