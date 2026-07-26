from __future__ import annotations

from datetime import datetime
import json
import hashlib
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import venv
import zipfile

from tests.resume.render_baseline import (
    _build_example_outputs,
    _normalize_html,
    _normalize_text,
    _parse_env_file,
    _tool_versions,
)
from tests.resume.pdf_visual_equivalence import PdfVisualThresholds

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_MANIFEST = REPO_ROOT / "tests" / "fixtures" / "resume" / "example-render-baseline" / "manifest.json"
DENIED_TEXT_FRAGMENTS = (
    "PRIVATE_PROFILE_SENTINEL",
    "PRIVATE_COMPANY_SENTINEL",
    "PRIVATE_CONFIG_SENTINEL",
    "/Users/teslamint/",
)
TEXT_SUFFIXES = {
    ".py", ".pyi", ".txt", ".md", ".html", ".css", ".js", ".json", ".toml", ".yml", ".yaml", ".ini", ".cfg", ".rst"
}
TEXT_BASENAMES = {"METADATA", "PKG-INFO", "entry_points.txt", "top_level.txt", "RECORD", "SOURCES.txt"}


def run(argv: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=cwd, env=env, text=True, capture_output=True, check=False)


def require_ok(result: subprocess.CompletedProcess[str], *, context: str) -> None:
    if result.returncode != 0:
        raise AssertionError(f"{context} failed with exit {result.returncode}\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")


def build_distributions(tmp_path: Path) -> tuple[Path, Path]:
    out_dir = tmp_path / "dist"
    result = run(["uv", "build", "--out-dir", str(out_dir)], cwd=REPO_ROOT)
    require_ok(result, context="uv build")
    wheel = next(out_dir.glob("careerkit-*.whl"))
    sdist = next(out_dir.glob("careerkit-*.tar.gz"))
    return wheel, sdist


def create_installed_venv(tmp_path: Path, wheel: Path) -> Path:
    venv_dir = tmp_path / "venv"
    venv.EnvBuilder(with_pip=True, system_site_packages=False).create(venv_dir)
    python_bin = venv_dir / "bin" / "python"
    install = run([str(python_bin), "-m", "pip", "install", str(wheel)], cwd=REPO_ROOT)
    require_ok(install, context="pip install wheel")
    return python_bin


def create_smoke_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "smoke-workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / ".career-workspace").write_text("1\n", encoding="utf-8")
    shutil.copytree(REPO_ROOT / "example", workspace / "example")
    (workspace / "private/jd/config").mkdir(parents=True, exist_ok=True)
    (workspace / "private/profile").mkdir(parents=True, exist_ok=True)
    (workspace / "private/companies/sentinelco").mkdir(parents=True, exist_ok=True)
    (workspace / "private/profile/summary-job.md").write_text("PRIVATE_PROFILE_SENTINEL", encoding="utf-8")
    (workspace / "private/companies/sentinelco/profile.md").write_text("PRIVATE_COMPANY_SENTINEL", encoding="utf-8")
    (workspace / "private/jd/config/search_config.yaml").write_text(
        (
            "search:\n"
            "  role: backend\n"
            "platforms:\n"
            "  wanted:\n"
            "    enabled: true\n"
            "  remember:\n"
            "    enabled: true\n"
            "  groupby:\n"
            "    enabled: true\n"
            "search_queries:\n"
            "  - 백엔드 엔지니어\n"
            "execution:\n"
            "  max_urls_per_run: 5\n"
        ),
        encoding="utf-8",
    )
    (workspace / "private/jd/config/jd-screening-rules.md").write_text("PRIVATE_CONFIG_SENTINEL\n", encoding="utf-8")
    return workspace


def installed_bin(python_bin: Path, name: str) -> Path:
    return python_bin.parent / name


def scan_text_for_denied(text: str, *, context: str) -> None:
    for fragment in DENIED_TEXT_FRAGMENTS:
        assert fragment not in text, f"{context} leaked forbidden fragment: {fragment}"


def is_text_member(name: str) -> bool:
    path = Path(name)
    return path.suffix in TEXT_SUFFIXES or path.name in TEXT_BASENAMES


def archive_text_members(wheel: Path, sdist: Path) -> list[tuple[str, str]]:
    members: list[tuple[str, str]] = []
    with zipfile.ZipFile(wheel) as archive:
        for name in archive.namelist():
            if is_text_member(name):
                members.append((f"wheel:{name}", archive.read(name).decode("utf-8", errors="ignore")))
    with tarfile.open(sdist, "r:gz") as archive:
        for member in archive.getmembers():
            if member.isfile() and is_text_member(member.name):
                extracted = archive.extractfile(member)
                if extracted is None:
                    continue
                members.append((f"sdist:{member.name}", extracted.read().decode("utf-8", errors="ignore")))
    return members


def load_example_manifest() -> dict[str, object]:
    return json.loads(EXAMPLE_MANIFEST.read_text(encoding="utf-8"))


def baseline_now(manifest: dict[str, object]) -> datetime | None:
    from datetime import datetime as _dt

    raw = manifest.get("baseline_now")
    return _dt.fromisoformat(str(raw)) if raw else None


def current_render_tool_versions() -> dict[str, str]:
    return _tool_versions()


def example_output_thresholds() -> PdfVisualThresholds:
    values = _parse_env_file(REPO_ROOT / "docker" / "render-versions.env")
    return PdfVisualThresholds(
        raster_dpi=int(values["PDF_RASTER_DPI"]),
        channel_threshold=int(values["PDF_PIXEL_CHANNEL_THRESHOLD"]),
        max_differing_pixel_ratio=float(values["PDF_MAX_DIFFERING_PIXEL_RATIO"]),
    )


def generate_example_outputs(tmp_path: Path, *, now: datetime | None = None) -> Path:
    workspace = tmp_path / "render-workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / ".career-workspace").write_text("1\n", encoding="utf-8")
    shutil.copytree(REPO_ROOT / "example", workspace / "example")
    build_dir = _build_example_outputs(workspace, now=now)
    return build_dir


def normalized_output(path: Path) -> str:
    raw = path.read_text(encoding="utf-8")
    return _normalize_html(raw) if path.suffix == ".html" else _normalize_text(raw)


def canonicalize_html_for_comparison(text: str) -> str:
    text = re.sub(r'(<html[^>]*?)\s+lang=""\s+xml:lang=""', r"\1", text)
    text = re.sub(r'<meta name="generator" content="pandoc[^"]*" />\n', "", text)
    text = re.sub(r"<tr class=\"(?:header|odd|even)\">", "<tr>", text)
    text = re.sub(
        r"<style>\n.*?\.display\.math\{display: block; text-align: center; margin: 0\.5rem auto;\}\n\s*</style>\n",
        "",
        text,
        count=1,
        flags=re.DOTALL,
    )
    return text


def canonicalize_text_for_comparison(text: str) -> str:
    canonical_lines: list[str] = []
    for line in text.splitlines():
        canonical_lines.append(re.sub(r"^\s*-\s+", "- ", line))
    return "\n".join(canonical_lines) + "\n"


def resource_probe_code() -> str:
    return (
        "import importlib.resources as r, json;"
        "payload={"
        "'prompt': r.files('careerkit.jobs.resources.prompts').joinpath('screening_system.txt').read_text(encoding='utf-8').splitlines()[0],"
        "'html': 'JD Console' in r.files('careerkit.jobs.console.static').joinpath('index.html').read_text(encoding='utf-8'),"
        "'js': '.textContent' in r.files('careerkit.jobs.console.static').joinpath('app.js').read_text(encoding='utf-8')"
        "};"
        "print(json.dumps(payload, ensure_ascii=False))"
    )


def podman_available() -> tuple[bool, str]:
    if shutil.which("podman") is None:
        return False, "podman not installed; install Podman and rerun container smoke"
    info = run(["podman", "info", "--format", "json"], cwd=REPO_ROOT)
    if info.returncode != 0:
        return False, "podman is installed but unavailable; start Podman and rerun container smoke"
    return True, ""


def render_lock_hash() -> str:
    digest = hashlib.sha256()
    digest.update((REPO_ROOT / "docker" / "render-versions.env").read_bytes())
    digest.update((REPO_ROOT / "docker" / "render.Dockerfile").read_bytes())
    return digest.hexdigest()[:12]
