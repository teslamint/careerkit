from __future__ import annotations

from importlib import resources
from pathlib import Path
import shutil
import subprocess
import tempfile


class RendererUnavailableError(RuntimeError):
    pass


def ensure_command(name: str) -> str:
    resolved = shutil.which(name)
    if resolved is None:
        raise RendererUnavailableError(f"required renderer command is not installed: {name}")
    return resolved


def theme_css_path(filename: str) -> Path:
    resource = resources.files("careerkit.resume.resources.themes.default").joinpath(filename)
    with resources.as_file(resource) as path:
        return path


def write_text_output(output_path: Path, content: str) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")
    return output_path


def markdown_to_plain(markdown_path: Path, output_path: Path) -> Path:
    pandoc = ensure_command("pandoc")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([pandoc, str(markdown_path), "-t", "plain", "-o", str(output_path)], check=True, capture_output=True, text=True)
    return output_path


def markdown_to_html(markdown_path: Path, output_path: Path, *, css_path: Path) -> Path:
    pandoc = ensure_command("pandoc")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([pandoc, str(markdown_path), "-o", str(output_path), "--standalone", f"--css={css_path}"], check=True, capture_output=True, text=True)
    return output_path


def html_to_pdf(html_path: Path, output_path: Path) -> Path:
    weasyprint = ensure_command("weasyprint")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([weasyprint, str(html_path), str(output_path)], check=True, capture_output=True, text=True)
    return output_path


def render_markdown_bundle(
    markdown_content: str,
    *,
    markdown_path: Path,
    html_path: Path,
    pdf_path: Path | None,
    css_filename: str,
    plain_text_path: Path | None = None,
    render_markdown_content: str | None = None,
    css_path: Path | None = None,
) -> None:
    write_text_output(markdown_path, markdown_content)
    render_source = markdown_path
    temporary_directory: tempfile.TemporaryDirectory[str] | None = None
    if render_markdown_content is not None:
        temporary_directory = tempfile.TemporaryDirectory()
        render_source = Path(temporary_directory.name) / "render.md"
        render_source.write_text(render_markdown_content, encoding="utf-8")
    try:
        selected_css = css_path or theme_css_path(css_filename)
        markdown_to_html(render_source, html_path, css_path=selected_css)
        if pdf_path is not None:
            html_to_pdf(html_path, pdf_path)
        if plain_text_path is not None:
            markdown_to_plain(markdown_path, plain_text_path)
    finally:
        if temporary_directory is not None:
            temporary_directory.cleanup()


def render_pdf_markdown(markdown_content: str, *, html_path: Path, pdf_path: Path, css_filename: str) -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_markdown = Path(temp_dir) / "render.md"
        temp_markdown.write_text(markdown_content, encoding="utf-8")
        css_path = theme_css_path(css_filename)
        markdown_to_html(temp_markdown, html_path, css_path=css_path)
        html_to_pdf(html_path, pdf_path)
