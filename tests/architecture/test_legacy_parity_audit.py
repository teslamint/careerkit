from __future__ import annotations

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT_INVENTORY = ROOT / "docs" / "architecture" / "entrypoint-inventory.md"
PARITY_AUDIT = ROOT / "docs" / "architecture" / "legacy-behavior-parity-audit.md"

_ENTRYPOINT_ROW = re.compile(
    r"^- path: (?P<path>[^|]+) \| kind: [^|]+ \| area: (?P<area>[^|]+) \|",
    re.MULTILINE,
)
_AUDIT_ROW = re.compile(
    r"^\| `(?P<path>[^`]+)` \| .* \| `(?P<result>[^`]+)` \|$",
    re.MULTILINE,
)
_ALLOWED_RESULTS = {
    "parity",
    "delete-with-evidence",
    "equivalent-no-op",
    "equivalent-invariant",
    "recorded-removal",
    "recorded-replacement",
}


def test_every_legacy_product_entrypoint_has_a_terminal_parity_disposition() -> None:
    inventory = ENTRYPOINT_INVENTORY.read_text(encoding="utf-8")
    expected = {
        match.group("path").strip()
        for match in _ENTRYPOINT_ROW.finditer(inventory)
        if match.group("area").strip() != "test-convenience"
        and not match.group("path").strip().startswith("src/careerkit/")
    }
    audit = PARITY_AUDIT.read_text(encoding="utf-8")
    rows = {
        match.group("path"): match.group("result")
        for match in _AUDIT_ROW.finditer(audit)
    }

    assert expected == set(rows)
    assert set(rows.values()) <= _ALLOWED_RESULTS
    assert "unresolved" not in rows.values()
