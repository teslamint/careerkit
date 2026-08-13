"""Provider-independent evidence checks for screening documents.

The consistency gate reads the model's own `구분`/`대조` labels, so it goes blind
whenever the model mislabels. These checks read the filesystem and the résumé
corpus instead: a cited `[source:]` path must resolve, and a `충족` row must name
at least one technology the résumé actually contains.

Pure functions only — no repository, provider, or workspace dependency.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
import re

MATCH_HEADING = "## 이력/경험 매칭"
KIND_VALUES = ("필수", "주요업무", "우대")
MATCH_VALUES = ("충족", "부분", "없음")
MIN_TOKEN_LENGTH = 3

TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
SOURCE_RE = re.compile(r"\[source:\s*([^\]]+)\]")
# Requirements express alternatives as `Java \| Kotlin`, so a raw split("|")
# would read the alternative as the next column. Both the reader and the
# demotion writer split on this, or the writer would rewrite the wrong cell.
CELL_SPLIT_RE = re.compile(r"(?<!\\)\|")
# A citation may carry a line reference or an anchor: `path.md:12`, `path.md#절`.
CITATION_SUFFIX_RE = re.compile(r"(?:#\S*|:L?\d+(?:-L?\d+)?)$")

# Tokens whose ABSENCE from the résumé carries no information about the
# requirement. A row whose every token is listed here has an empty token set, so
# check_rows skips it — the row is not verifiable by keyword, not verified and
# passed. Membership therefore means "this word cannot decide the requirement",
# never "this word is common".
#
# The test is what the requirement is ABOUT. "Software Engineering 경력 5년 이상"
# is about years of experience; software and engineering are connective tissue and
# their absence proves nothing. "TDD 경험" is about TDD itself, so an absent `tdd`
# is exactly the signal the guard reads — listing it would let an unsupported 충족
# reach 지원 추천 unchecked. Product and platform names fail the test for the same
# reason: `postgresql`, `linux`, `oracle`, `react` and `elasticsearch` each drive a
# live demotion in this corpus and each is deliberately absent from the set.
#
# 2026-08-13 added the 15 connective tokens below, after a Korean résumé was
# found to have none of the English category nouns an English-language
# requirement is written from, so every such row lost its 충족 claim. Not every
# such row was a false demotion — some were the guard correctly catching an
# overreaching claim — which is why `tdd`, `iot`, `apm`, `agile`, `sdlc`,
# `machine`, `learning`, `agent`, `premise`, `json`, `b2g`, `o2o`, `migration`
# and `deprecation` stay out (PR #7 review). A requirement can be about a
# migration or about a deprecation, so those words are subjects, not tissue.
#
# `database`, `databases` and `dbms` are the deliberate exception, and the reason
# is a limit of token matching rather than a claim that they are connective. The
# check compares literal tokens, so it cannot connect a requirement's hypernym to
# the hyponyms a résumé names. Keeping them skips an unsupported claim on a
# résumé with no database experience at all; removing them demotes a requirement
# naming a database in the abstract on a résumé full of them. The second failure
# is measured, the first is not, so the trade is made that way and revisited if a
# real case appears.
#
# `http`, `https`, `tcp`, `udp`, `rest`, `restful`, `b2b`, `b2c`, `b2b2c`, `saas`,
# `sql` and `api` read like the same gap — a requirement can be about HTTP or
# about B2B — and an earlier revision of this comment called removing them a
# worthwhile separate change because it raises the strict warning count. That
# reasoning was wrong. strict counts warnings; a row is demoted only when EVERY
# token is absent, and on the measured corpus removing the twelve took demoted
# rows from 1 to 7 while every added demotion was false, each the same hypernym
# gap `database` has. Do not raise strict as an improvement without checking what
# it demotes.
#
# The measurement, its method, the per-token breakdown and the rows it classified
# stay in the workspace repository under `docs/solutions/`, with the corpus they
# were run against.
GENERIC_TOKENS = frozenset(
    {
        "jd", "rdbms", "rdb", "nosql", "dbms", "database", "databases",
        "oop", "mvc", "sql", "api", "apis", "orm",
        "years", "architecture", "backend", "frontend", "web", "server", "cloud",
        "devops", "infra", "microservice", "microservices", "msa", "rest", "restful",
        "http", "https", "tcp", "udp", "ci", "cd",
        "llm", "ai", "ml", "db", "ux", "ui",
        "saas", "b2b", "b2c", "b2b2c", "poc", "kpi", "qa",
        "pm", "cto",
        "software", "engineering", "system", "systems", "production", "technical",
        "tool", "tools", "legacy",
        "and", "the", "for", "with", "based", "level", "senior", "junior", "lead",
        "plus", "etc", "first", "measure", "challenging",
    }
)


@dataclass(frozen=True)
class MatchRow:
    index: int
    requirement: str
    kind: str
    match: str
    evidence: str
    line_no: int


@dataclass(frozen=True)
class EvidenceReport:
    demoted_indices: tuple[int, ...] = ()
    missing_source_path: int = 0
    unevidenced_keyword: int = 0
    unevidenced_keyword_strict: int = 0


def _is_separator(line: str) -> bool:
    return set(line) <= set("|- :")


def _split_row(line: str) -> list[str]:
    """Split a table row on unescaped pipes, dropping the empty edges they leave."""
    parts = CELL_SPLIT_RE.split(line)
    if parts and not parts[0].strip():
        parts = parts[1:]
    if parts and not parts[-1].strip():
        parts = parts[:-1]
    return parts


def parse_match_table(markdown: str) -> tuple[list[MatchRow], str]:
    """Parse the contract table. Returns (rows, error_reason); error is '' on success."""
    start = markdown.find(MATCH_HEADING)
    if start < 0:
        return [], "매칭 표 없음"

    lines = markdown.splitlines()
    heading_index = markdown[:start].count("\n")
    rows: list[MatchRow] = []

    for offset, raw in enumerate(lines[heading_index + 1 :], start=heading_index + 1):
        line = raw.strip()
        if line.startswith("## "):
            break
        if not line or _is_separator(line):
            continue
        # The contract confines this section to the table. A gap described in a
        # bullet or sub-heading instead of a row is invisible to every check that
        # reads rows, so a recommendation could publish over gaps the document
        # itself spells out — non-table content fails, feeding the retry path.
        if not line.startswith("|"):
            return [], f"매칭 표 밖 내용: {line[:40]}"
        cells = [cell.strip() for cell in _split_row(line)]
        # Count checks run before the header skip: a header is a row too, and
        # skipping it first would let a two- or five-column header shape a table
        # whose data rows then pass as four columns.
        if len(cells) < 4:
            return [], "매칭 표 컬럼 부족"
        # R1 fixes the table at four columns. A fifth cell means an unescaped pipe
        # or an invented field, and only cells[3] would be read as evidence — a
        # citation shifted past it would escape the source check unnoticed.
        if len(cells) > 4:
            return [], "매칭 표 컬럼 초과"
        if cells[0] in {"요건", "JD 요건", "JD 요구사항"}:
            continue
        if cells[1] not in KIND_VALUES:
            return [], f"구분 칸 허용 밖 값: {cells[1]}"
        if cells[2] not in MATCH_VALUES:
            return [], f"대조 칸 허용 밖 값: {cells[2]}"
        rows.append(
            MatchRow(
                index=len(rows),
                requirement=cells[0],
                kind=cells[1],
                match=cells[2],
                evidence=cells[3],
                line_no=offset,
            )
        )

    if not rows:
        return [], "매칭 표 행 없음"

    return rows, ""


def extract_tokens(text: str) -> set[str]:
    """Lowercased ASCII runs of MIN_TOKEN_LENGTH+ characters, minus category terms."""
    found = {
        token.lower()
        for token in TOKEN_RE.findall(text)
        if len(token) >= MIN_TOKEN_LENGTH
    }
    return found - GENERIC_TOKENS


def corpus_basenames(corpus: str) -> set[str]:
    """File names usable as a bare-citation fallback: unique among declared sources.

    The basename fallback exists because 3.11% of real citations omit the
    directory prefix. It only identifies a source when exactly one declared path
    carries that name — every company contributes its own `profile.md`, so a bare
    or mis-prefixed `profile.md` names none of them and stays a violation.

    Known limit: this allowlist is derived from the same corpus that goes into the
    prompt, so a model can read a unique file name out of its context and attach
    it to an invented directory. Path checking is not the layer that catches
    invented *claims* — the keyword check below is, and it is independent of what
    the model cites.
    """
    counts = Counter(Path(path).name for path in corpus_source_paths(corpus))
    return {name for name, count in counts.items() if count == 1}


def corpus_source_paths(corpus: str) -> set[str]:
    """Exact paths the résumé corpus declares through its own [source: path] markers.

    These, not the workspace filesystem, are what a citation may point at: an
    existence check accepted any Markdown file in the repository, so a fabricated
    `docs/getting-started.md` citation resolved because the file happened to exist.
    """
    return {
        Path(CITATION_SUFFIX_RE.sub("", path.strip())).as_posix()
        for match in SOURCE_RE.finditer(corpus)
        for path in [match.group(1)]
        if path.strip()
    }


def _within_root(root: Path, candidate: str) -> bool:
    path = Path(candidate)
    if path.is_absolute():
        return False
    try:
        (root / path).resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return False
    return True


def resolve_source_paths(
    text: str, *, root: Path, declared: set[str], basenames: set[str]
) -> list[str]:
    """Return cited paths that resolve neither exactly nor by basename.

    Prose that is not a path claim (`프로젝트`, `다수`, `—`) is not a citation and
    is excluded rather than counted as a violation. Anything naming a Markdown
    file is a path claim, including a bare `skills-job.md` and a `path.md:12` or
    `path.md#절` reference — the directory prefix and the suffix are notation, not
    the claim. Exact paths resolve against what the corpus declares, never against
    the filesystem: any repository Markdown "exists", but only corpus sources are
    evidence.
    """
    unresolved: list[str] = []
    for match in SOURCE_RE.finditer(text):
        for piece in re.split(r"[,\s]+", match.group(1)):
            candidate = CITATION_SUFFIX_RE.sub("", piece.strip().strip("`,;"))
            if not candidate.endswith(".md"):
                continue
            # The citation is model-supplied. An absolute path would override the
            # join outright and `..` would walk out of the workspace, so anything
            # that does not stay under root is unresolved by definition — and it
            # must fail before the basename fallback, or `/etc/skills-job.md` would
            # borrow a real résumé file name to escape the check.
            if not _within_root(root, candidate):
                unresolved.append(candidate)
                continue
            if Path(candidate).as_posix() in declared:
                continue
            if Path(candidate).name in basenames:
                continue
            unresolved.append(candidate)
    return unresolved


def check_rows(
    rows: list[MatchRow],
    *,
    corpus: str,
    root: Path,
    declared: set[str],
    basenames: set[str],
) -> EvidenceReport:
    """Run the path and keyword checks, reporting which rows lose their 충족 claim."""
    corpus_lower = corpus.lower()
    demoted: set[int] = set()
    missing_source_path = 0
    unevidenced_keyword = 0
    unevidenced_keyword_strict = 0

    for row in rows:
        unresolved = resolve_source_paths(
            row.evidence, root=root, declared=declared, basenames=basenames
        )
        if unresolved:
            missing_source_path += len(unresolved)
            demoted.add(row.index)

        if row.match != "충족":
            continue
        tokens = extract_tokens(row.requirement)
        if not tokens:
            continue
        absent = [token for token in tokens if token not in corpus_lower]
        if not absent:
            continue
        unevidenced_keyword_strict += 1
        if len(absent) == len(tokens):
            unevidenced_keyword += 1
            demoted.add(row.index)

    return EvidenceReport(
        demoted_indices=tuple(sorted(demoted)),
        missing_source_path=missing_source_path,
        unevidenced_keyword=unevidenced_keyword,
        unevidenced_keyword_strict=unevidenced_keyword_strict,
    )


def apply_demotions(
    markdown: str,
    rows: list[MatchRow],
    demoted: tuple[int, ...],
) -> str:
    """Rewrite the 대조 cell of the named rows to 없음, leaving every other cell intact."""
    if not demoted:
        return markdown

    by_index = {row.index: row for row in rows}
    lines = markdown.splitlines(keepends=True)
    for index in demoted:
        row = by_index.get(index)
        if row is None:
            continue
        raw = lines[row.line_no]
        newline = raw[len(raw.rstrip("\r\n")) :]
        parts = CELL_SPLIT_RE.split(raw.rstrip("\r\n"))
        if len(parts) < 5:
            continue
        cell = parts[3]
        lead = cell[: len(cell) - len(cell.lstrip())]
        trail = cell[len(cell.rstrip()) :]
        parts[3] = f"{lead}없음{trail}"
        lines[row.line_no] = "|".join(parts) + newline
    return "".join(lines)
