from __future__ import annotations

import tarfile
import zipfile
from pathlib import Path


def test_distribution_allowlists() -> None:
    root = Path(__file__).resolve().parents[2]
    dist = root / "dist"
    wheel = next(dist.glob("careerkit-*.whl"))
    sdist = next(dist.glob("careerkit-*.tar.gz"))

    with zipfile.ZipFile(wheel) as archive:
        wheel_names = set(archive.namelist())

    assert "careerkit/__init__.py" in wheel_names
    assert "careerkit/workspace.py" in wheel_names
    assert "careerkit/resume/cli.py" in wheel_names
    assert "careerkit/jobs/cli.py" in wheel_names
    assert any(name.endswith("dist-info/entry_points.txt") for name in wheel_names)
    assert not any(name.startswith("templates/") for name in wheel_names)
    assert not any(name.startswith("tests/") for name in wheel_names)
    assert not any(name.startswith("private/") for name in wheel_names)
    assert not any(".career-workspace" in name for name in wheel_names)

    with tarfile.open(sdist, "r:gz") as archive:
        sdist_names = set(archive.getnames())

    assert any(name.endswith("/src/careerkit/workspace.py") for name in sdist_names)
    assert any(name.endswith("/pyproject.toml") for name in sdist_names)
    assert not any("/templates/" in name for name in sdist_names)
    assert not any("/tests/" in name for name in sdist_names)
    assert not any("/private/" in name for name in sdist_names)
