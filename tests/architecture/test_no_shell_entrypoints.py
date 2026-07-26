from __future__ import annotations

import subprocess

from .active_surfaces import ROOT


def test_no_repository_shell_entrypoints_survive_cutover() -> None:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "*.sh"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.splitlines() == []
