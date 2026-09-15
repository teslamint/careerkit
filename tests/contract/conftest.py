from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def built_distributions(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """Build the wheel and sdist from the current tree and return their paths.

    Distribution assertions describe the packaging configuration of the tree
    under test, so they build their own artifacts. Reading a checked-out
    ``dist/`` directory instead ties them to whoever ran ``uv build`` last: the
    directory is absent in a fresh clone or linked worktree, and a stale
    artifact keeps the allowlist and privacy checks passing after the
    configuration has already started shipping the wrong files.
    """
    from tests.contract._u8_final_proof import build_distributions

    return build_distributions(tmp_path_factory.mktemp("distributions"))
