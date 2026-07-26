from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any, cast

import pytest

from ._u8_final_proof import (
    REPO_ROOT,
    archive_text_members,
    build_distributions,
    canonicalize_html_for_comparison,
    canonicalize_text_for_comparison,
    current_render_tool_versions,
    create_installed_venv,
    create_smoke_workspace,
    example_output_thresholds,
    baseline_now,
    generate_example_outputs,
    installed_bin,
    load_example_manifest,
    normalized_output,
    podman_available,
    resource_probe_code,
    run,
    scan_text_for_denied,
    require_ok,
    render_lock_hash,
)
from tests.resume.render_baseline import _page_count, _pdftotext_normalized, _rasterize_pdf
from tests.resume.pdf_visual_equivalence import compare_ppm_pages


def _manifest_text_outputs(manifest: dict[str, object]) -> dict[str, dict[str, str]]:
    return cast(dict[str, dict[str, str]], manifest["text_outputs"])


def _manifest_pdf_outputs(manifest: dict[str, object]) -> dict[str, dict[str, Any]]:
    return cast(dict[str, dict[str, Any]], manifest["pdf_outputs"])


def test_built_archives_expose_only_allowed_surface_and_packaged_resources(tmp_path: Path) -> None:
    wheel, sdist = build_distributions(tmp_path)

    for label, text in archive_text_members(wheel, sdist):
        scan_text_for_denied(text, context=label)

    import tarfile
    import zipfile

    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    assert "careerkit/jobs/resources/prompts/screening_system.txt" in names
    assert "careerkit/jobs/console/static/index.html" in names
    assert "careerkit/jobs/console/static/app.js" in names
    assert "careerkit/jobs/console/static/styles.css" in names
    assert not any(name.startswith("private/") for name in names)
    assert not any(name.startswith("templates/") for name in names)

    with tarfile.open(sdist, "r:gz") as archive:
        sdist_names = set(archive.getnames())
    assert any(name.endswith("/src/careerkit/jobs/resources/prompts/screening_system.txt") for name in sdist_names)
    assert any(name.endswith("/src/careerkit/jobs/console/static/index.html") for name in sdist_names)


def test_installed_wheel_smokes_both_clis_from_unrelated_cwd_and_keeps_outputs_clean(tmp_path: Path) -> None:
    wheel, _ = build_distributions(tmp_path)
    python_bin = create_installed_venv(tmp_path, wheel)
    workspace = create_smoke_workspace(tmp_path)
    unrelated_cwd = tmp_path / "outside"
    unrelated_cwd.mkdir(parents=True, exist_ok=True)

    commands = [
        [str(installed_bin(python_bin, "career-resume")), "--help"],
        [str(installed_bin(python_bin, "career-jobs")), "--help"],
        [str(installed_bin(python_bin, "career-resume")), "--workspace", str(workspace), "validate", "--example"],
        [str(installed_bin(python_bin, "career-resume")), "--workspace", str(workspace), "build", "example", "wanted"],
        [str(installed_bin(python_bin, "career-jobs")), "--workspace", str(workspace), "config", "check", "--json"],
        [str(python_bin), "-c", resource_probe_code()],
    ]
    outputs: list[subprocess.CompletedProcess[str]] = []
    for argv in commands:
        result = run(argv, cwd=unrelated_cwd)
        outputs.append(result)

    for result in outputs[:-1]:
        require_ok(result, context="installed smoke command")
    config_check = outputs[-2]
    config_payload = json.loads(config_check.stdout)
    assert config_payload["command"] == "config check"
    assert config_payload["status"] == "ok"
    assert config_payload["normalized_role"] == "backend"
    assert config_payload["finding_codes"] == []

    payload = json.loads(outputs[-1].stdout)
    assert payload["prompt"].startswith("아래 기준으로 JD 스크리닝을 수행하세요.")
    assert payload["html"] is True
    assert payload["js"] is True

    for result in outputs:
        scan_text_for_denied(result.stdout, context="installed stdout")
        scan_text_for_denied(result.stderr, context="installed stderr")

    built_txt = (workspace / "example" / "build" / "resume-example-wanted.txt").read_text(encoding="utf-8")
    scan_text_for_denied(built_txt, context="resume-example-wanted.txt")


def test_current_example_outputs_match_captured_manifest_with_pdf_tolerance(tmp_path: Path) -> None:
    manifest = load_example_manifest()
    required_tools = ("pandoc", "weasyprint", "pdftotext", "pdftoppm", "pdfinfo")
    missing_tools = [tool for tool in required_tools if shutil.which(tool) is None]
    if missing_tools:
        pytest.skip(f"host render tools unavailable: {', '.join(missing_tools)}")

    manifest_tool_versions = cast(dict[str, str], manifest["tool_versions"])
    current_tool_versions = current_render_tool_versions()
    if current_tool_versions != manifest_tool_versions:
        pytest.skip(
            "pixel-equivalence requires the fixture render toolchain; "
            f"manifest={manifest_tool_versions}, current={current_tool_versions}"
        )

    build_dir = generate_example_outputs(tmp_path, now=baseline_now(manifest))
    thresholds = example_output_thresholds()
    raster_root = tmp_path / "rendered-pages"
    for relative, payload in _manifest_text_outputs(manifest).items():
        current = normalized_output(build_dir / relative)
        expected = payload["normalized_content"]
        if relative.endswith(".html"):
            current = canonicalize_html_for_comparison(current)
            expected = canonicalize_html_for_comparison(expected)
        elif relative.endswith(".txt"):
            current = canonicalize_text_for_comparison(current)
            expected = canonicalize_text_for_comparison(expected)
        assert current == expected

    raster_dpi = cast(int, manifest["raster_dpi"])
    for relative, payload in _manifest_pdf_outputs(manifest).items():
        pdf_path = build_dir / relative
        assert _pdftotext_normalized(pdf_path) == payload["normalized_pdftotext"]
        assert _page_count(pdf_path) == payload["page_count"]
        candidate_pages = _rasterize_pdf(
            pdf_path,
            raster_root / Path(relative).stem,
            dpi=raster_dpi,
            output_root=raster_root,
        )
        assert len(candidate_pages) == len(payload["pages"])
        for expected_page, candidate_page in zip(payload["pages"], candidate_pages, strict=True):
            expected_page_file = cast(str, expected_page["file"])
            candidate_page_file = cast(str, candidate_page["file"])
            baseline = REPO_ROOT / "tests" / "fixtures" / "resume" / "example-render-baseline" / expected_page_file
            candidate = raster_root / candidate_page_file
            comparison = compare_ppm_pages(baseline, candidate, thresholds, diagnostics_dir=tmp_path / "ppm-diagnostics")
            assert comparison.matches, f"{relative} {expected_page_file} mismatch: {comparison.reason}"


def test_render_container_smoke_uses_render_image_when_podman_is_available(tmp_path: Path) -> None:
    available, reason = podman_available()
    if not available:
        pytest.skip(reason)

    lock_hash = render_lock_hash()
    image_tag = f"careerkit-render-smoke:{lock_hash}"
    build_context = tmp_path / "podman-context"
    build_context.mkdir(parents=True, exist_ok=True)
    build = run(
        ["podman", "build", "-f", str(REPO_ROOT / "docker" / "render.Dockerfile"), "-t", image_tag, str(build_context)],
        cwd=REPO_ROOT,
    )
    require_ok(build, context="podman build render image")
    smoke = run(["podman", "run", "--rm", image_tag], cwd=REPO_ROOT)
    require_ok(smoke, context="podman run render image")
    combined = smoke.stdout + smoke.stderr
    assert "pandoc" in combined.lower()
    assert "weasyprint" in combined.lower()
    assert "pdftotext" in combined.lower()

    local_temp_root = REPO_ROOT / ".tmp"
    local_temp_root.mkdir(exist_ok=True)
    verification_root = Path(tempfile.mkdtemp(prefix="u8-render-", dir=local_temp_root))
    try:
        wheel, _ = build_distributions(verification_root)
        verify = run(
            [
                "podman",
                "run",
                "--rm",
                "--userns=keep-id",
                "-v",
                f"{REPO_ROOT / 'tests'}:/repo/tests:ro",
                "-v",
                f"{REPO_ROOT / 'example'}:/repo/example:ro",
                "-v",
                f"{REPO_ROOT / 'docker'}:/repo/docker:ro",
                "-v",
                f"{verification_root}:/verification",
                "-v",
                f"{wheel.parent}:/wheel:ro",
                "-w",
                "/repo",
                image_tag,
                "bash",
                "-lc",
                (
                    "python -m pip install --no-deps --target /verification/site /wheel/*.whl >/dev/null && "
                    "PYTHONPATH=/verification/site python -c \""
                    "from pathlib import Path; "
                    "from tests.contract.verify_render_baseline import verify_render_baseline; "
                    "verify_render_baseline(Path('/verification/render-proof'))\""
                ),
            ],
            cwd=REPO_ROOT,
        )
        require_ok(verify, context="pinned render baseline verification")
    finally:
        shutil.rmtree(verification_root, ignore_errors=True)


def test_application_container_uses_installed_clis_and_synthetic_context_only() -> None:
    available, reason = podman_available()
    if not available:
        pytest.skip(reason)

    image_tag = "careerkit-application-smoke:u8"
    build = run(["podman", "build", "-t", image_tag, "."], cwd=REPO_ROOT)
    require_ok(build, context="podman build application image")
    scan_text_for_denied(build.stdout + build.stderr, context="application image build")

    history = run(["podman", "history", "--no-trunc", image_tag], cwd=REPO_ROOT)
    require_ok(history, context="podman application image history")
    scan_text_for_denied(history.stdout + history.stderr, context="application image history")

    jobs_help = run(
        ["podman", "run", "--rm", "--entrypoint", "career-jobs", image_tag, "--help"],
        cwd=REPO_ROOT,
    )
    require_ok(jobs_help, context="containerized career-jobs help")

    local_temp_root = REPO_ROOT / ".tmp"
    local_temp_root.mkdir(exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix="u8-container-workspace-", dir=local_temp_root))
    try:
        (workspace / ".career-workspace").write_text("1\n", encoding="utf-8")
        shutil.copytree(REPO_ROOT / "example", workspace / "example", ignore=shutil.ignore_patterns("build"))
        resume_build = run(
            [
                "podman",
                "run",
                "--rm",
                "--userns=keep-id",
                "-v",
                f"{workspace}:/workspace",
                image_tag,
                "example",
                "wanted",
            ],
            cwd=REPO_ROOT,
        )
        require_ok(resume_build, context="containerized synthetic resume build")
        assert (workspace / "example/build/resume-example-wanted.txt").is_file()
        scan_text_for_denied(
            jobs_help.stdout + jobs_help.stderr + resume_build.stdout + resume_build.stderr,
            context="application container output",
        )
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
