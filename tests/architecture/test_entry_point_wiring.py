"""Guards against a feature becoming unreachable from the CLI while its unit tests stay green.

A sync-adopt once reverted the TheVC registration in `cli.py`: the adapter and its
48 unit tests survived, but the `--platform` choice and the handler branch were
lost, so nothing reached the adapter. The suite passed because the CLI integration
tests were dropped in the same overwrite.

Unit tests over an adapter prove the adapter works, never that anything reaches it.
These tests assert the wiring itself.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CLI_PATH = ROOT / "src/careerkit/jobs/cli.py"
CLI_TEST_PATH = ROOT / "tests/jobs/test_cli.py"


def _cli_source() -> str:
    return CLI_PATH.read_text(encoding="utf-8")


def _company_fetch_platform_choices() -> list[str]:
    """The literal platform list registered on `company fetch --platform`."""
    tree = ast.parse(_cli_source())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "add_argument":
            continue
        if not isinstance(func.value, ast.Name) or func.value.id != "company_fetch":
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant):
            continue
        if node.args[0].value != "--platform":
            continue
        for keyword in node.keywords:
            if keyword.arg == "choices" and isinstance(keyword.value, ast.List):
                return [
                    element.value
                    for element in keyword.value.elts
                    if isinstance(element, ast.Constant) and isinstance(element.value, str)
                ]
    raise AssertionError("company fetch --platform choices not found in cli.py")


def _company_fetch_dispatch_platforms() -> set[str]:
    """Platforms the `_handle_company_fetch` body actually branches on."""
    tree = ast.parse(_cli_source())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_handle_company_fetch":
            body = ast.get_source_segment(_cli_source(), node) or ""
            return set(re.findall(r'args\.platform ==\s*"(\w+)"', body))
    raise AssertionError("_handle_company_fetch not found in cli.py")


def test_every_company_fetch_choice_has_a_handler_branch() -> None:
    """A registered choice with no branch falls through to the error path at runtime.

    argparse accepts the value, then the handler's final `else` reports it as
    unsupported - a failure only reachable by running the command.
    """
    choices = set(_company_fetch_platform_choices())
    dispatched = _company_fetch_dispatch_platforms()

    assert choices - dispatched == set(), (
        f"registered but never dispatched: {sorted(choices - dispatched)}"
    )
    assert dispatched - choices == set(), (
        f"dispatched but not selectable via --platform: {sorted(dispatched - choices)}"
    )


def test_every_company_fetch_platform_is_driven_through_cli_main() -> None:
    """Each platform needs a test that calls `cli.main([...])` with it.

    Adapter unit tests do not catch a lost registration. This is the assertion
    that fails when the wiring disappears.
    """
    test_source = CLI_TEST_PATH.read_text(encoding="utf-8")
    driven = set(
        re.findall(
            r"cli\.main\(\[[^\]]*'company'[^\]]*'fetch'[^\]]*'--platform',\s*'(\w+)'",
            test_source,
        )
    )

    missing = set(_company_fetch_platform_choices()) - driven
    assert missing == set(), (
        f"no cli.main() integration test drives: {sorted(missing)} - "
        "an adapter unit test does not prove the CLI reaches it"
    )
