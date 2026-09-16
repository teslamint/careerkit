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
import json
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
    r"(engineer|developer|backend|frontend|server|엔지니어|개발자|백엔드|프론트|서버|비공개)",
    re.IGNORECASE,
)
_VALUE_SPLIT = re.compile(r"\s*[-/|,·]\s*|\s*[\(\)\[\]]\s*|\s+")
_R2_BOUND = r"(?:^|[\s`\"'\(\)\[\]·,/|=:]|\(주\))"
_R2_TRAIL = (
    r"(?:$|[\s`\"'\(\)\[\]·,/|=:]"
    r"|은|는|이|가|을|를|에|의|에서|으로|와|과|도|만|사|팀|측|라는|입니다|이다)"
)
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
    r"https?://[^\s]*?(?:wanted\.kr|saramin\.co\.kr|jobkorea\.co\.kr|jumpit\.co\.kr"
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
_ENTITY_POSITION = re.compile(r"(?:합류|스카웃|영입|이직|재직)\s*(?:했|하|해)")
_QUOTED = re.compile(
    r"[`\"'\u201c\u2018]([^`\"'\u201d\u2019]{12,}[가-힣][^`\"'\u201d\u2019]{0,200})"
    r"[`\"'\u201d\u2019]"
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
    }
)
_TERM_FILE = re.compile(r"publish-guard-terms\.json")


def _layer1(text: str, line: int) -> Iterator[Finding]:
    for match in _PRIVATE_NARROW.finditer(text):
        yield Finding("private-path", match.group(0), line)
    for match in re.finditer(r"(?:~|/Users/|/home/)[^\s]*", text):
        if "private" in match.group(0):
            yield Finding("private-path", match.group(0), line)
    for match in re.finditer(
        r"(?<![\w/-])(?P<platform>[a-z][a-z0-9]*)/(?P<key>[0-9]{2,}|[A-Za-z0-9-]{20,})(?![\w/-])",
        text,
    ):
        if match.group("platform") in platform_names():
            yield Finding("record-key", match.group(0), line)
    for match in re.finditer(
        r"https?://[^\s]*?(?:wanted\.kr|saramin\.co\.kr|jobkorea\.co\.kr|jumpit\.co\.kr"
        r"|rememberapp\.co\.kr|groupby\.co\.kr|offercent\.kr|greeting\.hr)[^\s]*",
        text,
        re.IGNORECASE,
    ):
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
        r"[가-힣A-Za-z0-9]{1,20}\s*(?:그룹|홀딩스|인더스트리|테크|랩스|웍스|컴퍼니)", text
    ):
        head = re.match(r"[가-힣A-Za-z0-9]+", match.group(0))
        if head and head.group(0) not in _GENERIC_SUBJECTS:
            yield Finding("entity-suffix", match.group(0), line)
    for match in _ENTITY_POSITION.finditer(text):
        prefix = text[max(0, match.start() - 24) : match.start()]
        if not any(word in prefix for word in _GENERIC_SUBJECTS):
            yield Finding("entity-position", match.group(0), line)
    for match in _QUOTED.finditer(text):
        fragment = match.group(1)
        if len(fragment) >= 12 and _KOREAN_ENDINGS.search(fragment) and not _QUESTION_ENDINGS.search(fragment):
            yield Finding("quoted-corpus-prose", match.group(0), line)
    for match in _TERM_FILE.finditer(text):
        yield Finding("term-file", match.group(0), line)


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


def vocabulary_cache_path() -> Path | None:
    common = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"], capture_output=True, check=False
    ).stdout.decode("utf-8", errors="replace").strip()
    if not common:
        return None
    return Path(common) / "publish-guard-vocabulary"


def build_vocabulary(base: str) -> frozenset[str]:
    """NNP forms of the merge-base tree object plus that base's commit messages."""
    if not base:
        raise SystemExit("layer 2 base ref does not resolve; refusing an empty vocabulary")
    cache = vocabulary_cache_path()
    tree = subprocess.run(
        ["git", "rev-parse", f"{base}^{{tree}}"], capture_output=True, check=False
    ).stdout.decode("utf-8", errors="replace").strip()
    if cache and cache.is_file():
        try:
            cached = json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cached = None
        if cached and cached.get("key") == tree:
            return frozenset(cached["forms"])
    try:
        from kiwipiepy import Kiwi
    except ImportError:
        return frozenset()
    kiwi = Kiwi()
    forms: set[str] = set()
    for entry in subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", base], capture_output=True, check=False
    ).stdout.decode("utf-8", errors="replace").splitlines():
        rel = entry.strip()
        if not rel or Path(rel).suffix.lower() not in _TEXT_SUFFIXES:
            continue
        blob = subprocess.run(
            ["git", "show", f"{base}:{rel}"], capture_output=True, check=False
        ).stdout
        try:
            blob_text = blob.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if blob_text and re.search(r"[가-힣]", blob_text):
            forms |= _nnp_forms(kiwi, blob_text)
    messages = subprocess.run(
        ["git", "log", "-200", "--format=%B", base], capture_output=True, check=False
    ).stdout.decode("utf-8", errors="replace")
    if messages:
        forms |= _nnp_forms(kiwi, messages)
    if cache:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(
            json.dumps({"key": tree, "forms": sorted(forms)}), encoding="utf-8"
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
    return _build_terms(values)


def _build_terms(values: list[str]) -> Terms:
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
                ):
                    substring.add(piece)
        elif len(value) >= 4 and re.search(r"[가-힣]", value):
            substring.add(value)
        elif 2 <= len(value) <= 3:
            bounded.add(value)
    return Terms(frozenset(substring), frozenset(bounded))


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
            terms = Terms(frozenset(payload["substring"]), frozenset(payload["bounded"]))
            if terms.substring or terms.bounded:
                return terms, key
    terms = generate_terms(ws)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps(
            {
                "key": key,
                "substring": sorted(terms.substring),
                "bounded": sorted(terms.bounded),
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
    if 1 in layers or 3 in layers:
        for offset, block in enumerate(normalized.splitlines() or [normalized]):
            line = base + offset
            if 1 in layers:
                yield from _layer1(block, line)
            if 3 in layers:
                yield from _layer3(block, terms, line)
    if 2 in layers and analyzer is not None:
        yield from _layer2(normalized, analyzer, vocabulary, base)


# --- CLI --------------------------------------------------------------------

def _git_output(*command: str) -> str:
    return subprocess.run(
        ["git", *command], capture_output=True, check=False
    ).stdout.decode("utf-8", errors="replace")


def _parse_added(diff: str) -> list[tuple[str, int, str]]:
    lines: list[tuple[str, int, str]] = []
    current: str | None = None
    line_no = 0
    for raw in diff.splitlines():
        if raw.startswith("+++ b/"):
            current = raw[6:]
            continue
        if raw.startswith("@@"):
            match = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", raw)
            line_no = int(match.group(1)) if match else 0
            continue
        if raw.startswith("+") and not raw.startswith("+++") and current:
            lines.append((current, line_no, raw[1:]))
            line_no += 1
    return lines


def _added_commit_paths(base: str, head: str) -> list[str]:
    paths: list[str] = []
    for sha in _git_output("rev-list", f"{base}..{head}").split():
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
        try:
            from kiwipiepy import Kiwi
        except ImportError:
            print(
                "analyzer inactive: kiwipiepy is not installed; layer 2 skipped",
                file=sys.stderr,
            )
        else:
            base = _merge_base_ref(Path.cwd())
            if not base:
                print("layer 2 base ref does not resolve; layer 2 skipped", file=sys.stderr)
            else:
                analyzer = Kiwi()
                vocabulary = build_vocabulary(base)

    findings: list[Finding] = []
    if args.generate_terms:
        generated = generate_terms(args.generate_terms)
        key = terms_cache_key(args.generate_terms)
        print(
            f"terms: {len(generated.substring)} substring + {len(generated.bounded)} bounded; "
            f"key {key}"
        )
        return 0

    if args.message:
        path = Path(args.message)
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            print(f"cannot read {path}: {error}", file=sys.stderr)
            return 2
        findings = list(
            scan_text(content, terms=terms, layers=frozenset({1, 2, 3}), analyzer=analyzer, vocabulary=vocabulary)
        )
    elif args.staged:
        diff = _git_output(
            "diff", "--cached", "--no-ext-diff", "--no-color", "--no-textconv", "-U0"
        )
        added = _parse_added(diff)
        for path_name, line_no, content in added:
            findings.extend(
                scan_text(content, terms=terms, layers=frozenset({1, 2, 3}), analyzer=None, base=line_no)
            )
        names = _git_output("diff", "--cached", "--name-status")
        binary_count = 0
        for row in names.splitlines():
            path_name = row.split("\t")[-1]
            if Path(path_name).suffix.lower() in _BINARY_SUFFIXES:
                binary_count += 1
                continue
            findings.extend(
                scan_text(normalize_text(path_name), terms=terms, layers=frozenset({1, 2, 3}), analyzer=None, base=0)
            )
        if binary_count:
            if args.no_evidence:
                print(f"unscannable binary paths: {binary_count}", file=sys.stderr)
            else:
                print(
                    f"unscannable binary paths: {binary_count} (pass --no-evidence to see a count only)",
                    file=sys.stderr,
                )
    elif args.commits:
        if ".." not in args.commits:
            print("--commits expects BASE..HEAD", file=sys.stderr)
            return 2
        base, _, head = args.commits.partition("..")
        resolution = subprocess.run(
            ["git", "rev-parse", "--verify", base], capture_output=True, check=False
        )
        if resolution.returncode != 0:
            print(f"range {base}..{head} does not resolve", file=sys.stderr)
            return 2
        messages = _git_output("log", f"{base}..{head}", "--format=%B")
        findings.extend(
            scan_text(messages, terms=terms, layers=frozenset({1, 2, 3}), analyzer=analyzer, vocabulary=vocabulary)
        )
        added = _parse_added(
            _git_output(
                "diff", f"{base}..{head}", "--no-ext-diff", "--no-color", "--no-textconv", "-U0"
            )
        )
        for path_name, line_no, content in added:
            findings.extend(
                scan_text(content, terms=terms, layers=frozenset({1, 2, 3}), analyzer=None, base=line_no)
            )
        for path_name in _added_commit_paths(base, head):
            findings.extend(
                scan_text(normalize_text(path_name), terms=terms, layers=frozenset({1, 2, 3}), analyzer=None, base=0)
            )
    else:
        parser.print_usage(sys.stderr)
        return 2

    if not findings:
        return 0
    for finding in findings:
        if args.no_evidence:
            print(f"line {finding.line}: [{finding.rule}]", file=sys.stderr)
        else:
            print(f"line {finding.line}: [{finding.rule}] {finding.evidence}", file=sys.stderr)
    return 1


import os  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())