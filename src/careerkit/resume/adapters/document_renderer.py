from __future__ import annotations

from importlib import resources
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unicodedata
from urllib.parse import unquote


class _SubmissionTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def validate_submission_content(content: str) -> None:
    """Reject explicit research material, not ordinary citations or technical caveats."""
    decoded = content
    while True:
        next_decoded = unescape(unquote(decoded))
        if next_decoded == decoded:
            break
        decoded = next_decoded
    decoded = decoded.replace("\\", "/")
    normalized = "".join(
        character for character in unicodedata.normalize("NFKC", decoded)
        if unicodedata.category(character) != "Cf"
    )
    parser = _SubmissionTextParser()
    parser.feed(normalized)
    visible = "".join(parser.parts)
    patterns = (
        r"portfolio-review-only",
        r"\bevidence-[a-z0-9]+[-_][a-z0-9][a-z0-9_-]*",
        r"\[\s*Evidence\s*:",
        r"private/(?:portfolio-research|companies|profile)(?:/|\b)",
        r"(?:file:///|/(?:Users|home)/)[^\s<>\"']*/private/",
        r"출처\s*및\s*검토\s*메모",
    )
    if any(re.search(pattern, text, re.IGNORECASE) for text in (normalized, visible) for pattern in patterns):
        raise ValueError("submission contains research-only material; move it to a separate review document")


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
    validate_submission_content(content)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")
    return output_path


def markdown_to_plain(markdown_path: Path, output_path: Path) -> Path:
    validate_submission_content(markdown_path.read_text(encoding="utf-8"))
    pandoc = ensure_command("pandoc")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output_path.parent) as directory:
        staged = Path(directory) / output_path.name
        subprocess.run([pandoc, str(markdown_path), "-t", "plain", "-o", str(staged)], check=True, capture_output=True, text=True)
        validate_submission_content(staged.read_text(encoding="utf-8"))
        staged.replace(output_path)
    return output_path


def markdown_to_html(markdown_path: Path, output_path: Path, *, css_path: Path, title: str) -> Path:
    validate_submission_content(markdown_path.read_text(encoding="utf-8"))
    pandoc = ensure_command("pandoc")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output_path.parent) as directory:
        staged = Path(directory) / output_path.name
        subprocess.run(
            [
                pandoc,
                str(markdown_path),
                "-o",
                str(staged),
                "--standalone",
                f"--css={css_path}",
                f"--metadata=pagetitle:{title}",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        validate_submission_content(staged.read_text(encoding="utf-8"))
        staged.replace(output_path)
    return output_path


def html_to_pdf(html_path: Path, output_path: Path) -> Path:
    validate_submission_content(html_path.read_text(encoding="utf-8"))
    weasyprint = ensure_command("weasyprint")
    pdftotext = ensure_command("pdftotext")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output_path.parent) as directory:
        staged = Path(directory) / output_path.name
        subprocess.run([weasyprint, str(html_path), str(staged)], check=True, capture_output=True, text=True)
        try:
            extracted = subprocess.run([pdftotext, str(staged), "-"], check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError:
            raise ValueError("submission PDF text extraction failed; output was not replaced") from None
        if not extracted.stdout.strip():
            raise ValueError("submission PDF has no extractable text; output was not replaced")
        validate_submission_content(extracted.stdout)
        staged.replace(output_path)
    return output_path


def render_markdown_bundle(
    markdown_content: str,
    *,
    markdown_path: Path,
    html_path: Path,
    pdf_path: Path | None,
    css_filename: str,
    title: str,
    plain_text_path: Path | None = None,
    render_markdown_content: str | None = None,
    css_path: Path | None = None,
) -> None:
    validate_submission_content(markdown_content)
    if render_markdown_content is not None:
        validate_submission_content(render_markdown_content)
    write_text_output(markdown_path, markdown_content)
    render_source = markdown_path
    temporary_directory: tempfile.TemporaryDirectory[str] | None = None
    if render_markdown_content is not None:
        temporary_directory = tempfile.TemporaryDirectory()
        render_source = Path(temporary_directory.name) / "render.md"
        render_source.write_text(render_markdown_content, encoding="utf-8")
    try:
        selected_css = css_path or theme_css_path(css_filename)
        markdown_to_html(render_source, html_path, css_path=selected_css, title=title)
        if pdf_path is not None:
            html_to_pdf(html_path, pdf_path)
        if plain_text_path is not None:
            markdown_to_plain(markdown_path, plain_text_path)
    finally:
        if temporary_directory is not None:
            temporary_directory.cleanup()


def render_pdf_markdown(
    markdown_content: str,
    *,
    html_path: Path,
    pdf_path: Path,
    css_filename: str,
    title: str,
) -> None:
    validate_submission_content(markdown_content)
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_markdown = Path(temp_dir) / "render.md"
        temp_markdown.write_text(markdown_content, encoding="utf-8")
        css_path = theme_css_path(css_filename)
        markdown_to_html(temp_markdown, html_path, css_path=css_path, title=title)
        html_to_pdf(html_path, pdf_path)
