from __future__ import annotations

import tarfile
import zipfile
from pathlib import Path


WHEEL_DENIED_FRAGMENTS = (
    "private/",
    ".career-workspace",
    "/Users/teslamint/",
    "job_postings/",
    "jd_analysis/",
)

SDIST_DENIED_FRAGMENTS = (
    "private/",
    "/Users/teslamint/",
    "job_postings/",
    "jd_analysis/",
)


def test_built_distributions_do_not_expose_private_fragments() -> None:
    root = Path(__file__).resolve().parents[2]
    dist = root / "dist"
    wheel = next(dist.glob("careerkit-*.whl"))
    sdist = next(dist.glob("careerkit-*.tar.gz"))

    with zipfile.ZipFile(wheel) as archive:
        wheel_names = archive.namelist()
    with tarfile.open(sdist, "r:gz") as archive:
        sdist_names = archive.getnames()

    for fragment in WHEEL_DENIED_FRAGMENTS:
        assert not any(fragment in name for name in wheel_names)
    for fragment in SDIST_DENIED_FRAGMENTS:
        assert not any(fragment in name for name in sdist_names)
