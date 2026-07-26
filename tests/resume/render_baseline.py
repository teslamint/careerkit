from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


TEXT_OUTPUTS = (
    "resume-example.md",
    "resume-example.html",
    "resume-example-remember.txt",
    "resume-example-short.md",
    "resume-example-short.html",
    "resume-example-wanted.txt",
    "career-description-example.md",
    "career-description-example.html",
)

PDF_OUTPUTS = (
    "resume-example.pdf",
    "resume-example-short.pdf",
    "career-description-example.pdf",
)

VERSION_ARG_KEYS = (
    "PYTHON_BASE_IMAGE",
    "PANDOC_APT_PACKAGE",
    "POPPLER_APT_PACKAGE",
    "NOTO_CJK_APT_PACKAGE",
    "PANGO_APT_PACKAGE",
    "PANGOFT2_APT_PACKAGE",
    "WEASYPRINT_PIP_VERSION",
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def _normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized_lines = [line.rstrip() for line in text.split("\n")]
    normalized = "\n".join(normalized_lines).strip()
    return normalized + "\n"


def _normalize_html(text: str) -> str:
    normalized = _normalize_text(text)
    normalized = re.sub(
        r'href="[^"]*/(style(?:-short|-career)?\.css)"',
        r'href="THEME_CSS/\1"',
        normalized,
    )
    return normalized


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_text(text: str) -> str:
    return _sha256_bytes(text.encode("utf-8"))


def _run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=cwd, env=env, check=True, text=True, capture_output=True)


def _tool_versions() -> dict[str, str]:
    return {
        "python": _run(["python", "--version"]).stdout.strip(),
        "pandoc": _run(["pandoc", "--version"]).stdout.splitlines()[0].strip(),
        "weasyprint": _run(["weasyprint", "--version"]).stdout.strip(),
        "pdftotext": (_run(["pdftotext", "-v"]).stderr or _run(["pdftotext", "-v"]).stdout).splitlines()[0].strip(),
        "pdftoppm": (_run(["pdftoppm", "-v"]).stderr or _run(["pdftoppm", "-v"]).stdout).splitlines()[0].strip(),
        "pdfinfo": (_run(["pdfinfo", "-v"]).stderr or _run(["pdfinfo", "-v"]).stdout).splitlines()[0].strip(),
    }


def _page_count(pdf_path: Path) -> int:
    output = _run(["pdfinfo", str(pdf_path)]).stdout
    for line in output.splitlines():
        if line.startswith("Pages:"):
            return int(line.split(":", 1)[1].strip())
    raise RuntimeError(f"Pages not found in pdfinfo output for {pdf_path}")


def _pdftotext_normalized(pdf_path: Path) -> str:
    result = _run(["pdftotext", str(pdf_path), "-"])
    return _normalize_text(result.stdout)


def _rasterize_pdf(
    pdf_path: Path,
    destination_dir: Path,
    *,
    dpi: int,
    output_root: Path,
) -> list[dict[str, object]]:
    destination_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_prefix = Path(temp_dir) / "page"
        _run(["pdftoppm", "-r", str(dpi), str(pdf_path), str(temp_prefix)])
        for temp_path in sorted(Path(temp_dir).glob("page-*.ppm")):
            shutil.copy2(temp_path, destination_dir / temp_path.name)
    pages: list[dict[str, object]] = []
    for path in sorted(destination_dir.glob("page-*.ppm")):
        pages.append(
            {
                "file": str(path.relative_to(output_root)),
                "sha256": _sha256_bytes(path.read_bytes()),
            }
        )
    return pages


def _seed_workspace(root: Path, *, source_example_dir: Path) -> Path:
    workspace_root = root / "workspace"
    workspace_root.mkdir(parents=True, exist_ok=True)
    (workspace_root / ".career-workspace").write_text("1\n", encoding="utf-8")
    shutil.copytree(source_example_dir, workspace_root / "example")
    return workspace_root


def _build_example_outputs(workspace_root: Path, *, now: datetime | None = None) -> Path:
    from careerkit.resume.adapters.document_renderer import (
        render_markdown_bundle,
        render_pdf_markdown,
        write_text_output,
    )
    from careerkit.resume.adapters.filesystem import ResumeWorkspaceAdapter
    from careerkit.resume.application.build import ResumeBuildService
    from careerkit.resume.application.career_description import build_career

    base_dir = workspace_root / "example"
    build_dir = base_dir / "build"
    build_dir.mkdir(parents=True, exist_ok=True)
    service = ResumeBuildService(ResumeWorkspaceAdapter(base_dir=base_dir))

    full_prefix = build_dir / "resume-example"
    render_markdown_bundle(
        service.build_full("public"),
        markdown_path=full_prefix.with_suffix(".md"),
        html_path=full_prefix.with_suffix(".html"),
        pdf_path=full_prefix.with_suffix(".pdf"),
        css_filename="style.css",
        plain_text_path=build_dir / "resume-example-remember.txt",
    )

    short_prefix = build_dir / "resume-example-short"
    write_text_output(short_prefix.with_suffix(".md"), service.build_short("public"))
    render_pdf_markdown(
        service.build_short_pdf("public"),
        html_path=short_prefix.with_suffix(".html"),
        pdf_path=short_prefix.with_suffix(".pdf"),
        css_filename="style-short.css",
    )

    write_text_output(build_dir / "resume-example-wanted.txt", service.build_wanted("public", now=now))

    career_prefix = build_dir / "career-description-example"
    render_markdown_bundle(
        build_career(service.adapter, "public", now=now),
        markdown_path=career_prefix.with_suffix(".md"),
        html_path=career_prefix.with_suffix(".html"),
        pdf_path=career_prefix.with_suffix(".pdf"),
        css_filename="style-career.css",
        render_markdown_content=build_career(service.adapter, "public", format_type="pdf", now=now),
    )

    return build_dir


def _capture_manifest(
    *,
    repo_root: Path,
    output_dir: Path,
    image_id: str,
) -> dict[str, object]:
    render_values = _parse_env_file(repo_root / "docker" / "render-versions.env")
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace_root = _seed_workspace(Path(temp_dir), source_example_dir=repo_root / "example")
        build_dir = _build_example_outputs(workspace_root)

        texts: dict[str, dict[str, str]] = {}
        for relative in TEXT_OUTPUTS:
            raw = (build_dir / relative).read_text(encoding="utf-8")
            normalized = _normalize_html(raw) if relative.endswith(".html") else _normalize_text(raw)
            texts[relative] = {
                "normalized_content": normalized,
                "normalized_sha256": _sha256_text(normalized),
            }

        pdfs: dict[str, dict[str, object]] = {}
        raster_root = output_dir / "pages"
        if raster_root.exists():
            shutil.rmtree(raster_root)
        for relative in PDF_OUTPUTS:
            pdf_path = build_dir / relative
            page_dir = raster_root / pdf_path.stem
            pages = _rasterize_pdf(
                pdf_path,
                page_dir,
                dpi=int(render_values["PDF_RASTER_DPI"]),
                output_root=output_dir,
            )
            pdf_text = _pdftotext_normalized(pdf_path)
            pdfs[relative] = {
                "normalized_pdftotext": pdf_text,
                "normalized_pdftotext_sha256": _sha256_text(pdf_text),
                "page_count": _page_count(pdf_path),
                "pages": pages,
            }

    return {
        "schema_version": 1,
        "synthetic": False,
        "workspace_source": "example/",
        "image_id": image_id,
        "tool_versions": _tool_versions(),
        "expected_filenames": sorted([*TEXT_OUTPUTS, *PDF_OUTPUTS]),
        "raster_dpi": int(render_values["PDF_RASTER_DPI"]),
        "baseline_now": datetime.now().isoformat(timespec="seconds"),
        "text_outputs": texts,
        "pdf_outputs": pdfs,
    }


def _inside_container(output_dir: Path, image_id: str) -> None:
    repo_root = _repo_root()
    output_dir.mkdir(parents=True, exist_ok=True)
    normalized_image_id = image_id if image_id.startswith("sha256:") else f"sha256:{image_id}"
    manifest = _capture_manifest(repo_root=repo_root, output_dir=output_dir, image_id=normalized_image_id)
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _default_capture(output_dir: Path) -> None:
    repo_root = _repo_root()
    values = _parse_env_file(repo_root / "docker" / "render-versions.env")
    lock_hash = hashlib.sha256(
        (repo_root / "docker" / "render-versions.env").read_bytes()
        + (repo_root / "docker" / "render.Dockerfile").read_bytes()
    ).hexdigest()[:12]
    image_tag = f"careerkit-render-baseline:{lock_hash}"
    build_cmd = ["podman", "build", "-f", "docker/render.Dockerfile", "-t", image_tag]
    for key in VERSION_ARG_KEYS:
        build_cmd.extend(["--build-arg", f"{key}={values[key]}"])
    build_cmd.append(".")
    subprocess.run(build_cmd, cwd=repo_root, check=True)
    image_id = _run(["podman", "image", "inspect", "--format", "{{.Id}}", image_tag], cwd=repo_root).stdout.strip()

    output_path = f"/repo/{output_dir.relative_to(repo_root)}"
    container_cmd = [
        "podman",
        "run",
        "--rm",
        "--userns=keep-id",
        "-v",
        f"{repo_root}:/repo",
        "-w",
        "/repo",
        image_tag,
        "bash",
        "-lc",
        (
            "PYTHONPATH=/repo/src:/repo python -c \""
            "from pathlib import Path; "
            "from tests.resume.render_baseline import _inside_container; "
            f"_inside_container(Path('{output_path}'), '{image_id}')\""
        ),
    ]
    subprocess.run(container_cmd, cwd=repo_root, check=True)
    print(image_id)


def capture_example_render_baseline(output_dir: Path | None = None) -> None:
    """Capture the synthetic example baseline with the pinned render image."""
    repo_root = _repo_root()
    target = output_dir or repo_root / "tests/fixtures/resume/example-render-baseline"
    if not target.is_absolute():
        target = repo_root / target
    _default_capture(target)
