from pathlib import Path
import errno
import subprocess

import pytest

from careerkit.resume.adapters import document_renderer as renderer
from careerkit.resume import cli

from .test_cli_and_resources import _seed_example_workspace, _workspace


@pytest.mark.parametrize("mode", ["full", "short", "wanted", "career", "packet", "all"])
def test_cli_rejects_contaminated_submission_in_every_mode(tmp_path, capsys, mode):
    root = _workspace(tmp_path)
    _seed_example_workspace(root)
    contact = root / "example/profile/contact.md"
    contact.write_text(contact.read_text().replace("Example User", "evidence-synthetic-01"))
    assert cli.main(["--workspace", str(root), "build", "example", mode]) == 2
    diagnostic = capsys.readouterr().err
    assert "submission" in diagnostic
    assert "evidence-synthetic-01" not in diagnostic


@pytest.mark.parametrize("content", [
    "A [Evidence: evidence-lane3-04]",
    "<!-- portfolio-review-only -->",
    "## 출처 및 검토 메모",
    "private/portfolio-research/notes.md",
    "file:///Users/sample/private/notes.md",
    "/Users/sample/resume/private/profile/contact.md",
    "private/companies/sample/profile.md",
    "private/profile/summary.md",
    "private/profi\u200ble/summary.md",
    "private%252Fprofile%252Fsummary.md",
    "<span>evidence-</span>lane3-04",
])
def test_submission_rejects_review_material_without_overwriting(tmp_path, content):
    output = tmp_path / "resume.txt"
    output.write_text("previous")
    with pytest.raises(ValueError, match="submission"):
        renderer.write_text_output(output, content)
    assert output.read_text() == "previous"


def test_empty_pdf_extraction_preserves_output(tmp_path, monkeypatch):
    source = tmp_path / "source.html"
    source.write_text("<p>Safe text</p>")
    output = tmp_path / "resume.pdf"
    output.write_text("previous")
    monkeypatch.setattr(renderer, "ensure_command", lambda name: name)

    def run(args, **kwargs):
        if args[0] != "pdftotext":
            Path(args[-1]).write_text("pdf")
        return subprocess.CompletedProcess(args, 0, "\f\n", "")

    monkeypatch.setattr(renderer.subprocess, "run", run)
    with pytest.raises(ValueError, match="PDF"):
        renderer.html_to_pdf(source, output)
    assert output.read_text() == "previous"


@pytest.mark.parametrize("kind", ["bundle", "pdf-only"])
def test_bundle_renderers_preserve_outputs_on_pdf_failure(tmp_path, monkeypatch, kind):
    markdown = tmp_path / "resume.md"
    html = tmp_path / "resume.html"
    pdf = tmp_path / "resume.pdf"
    plain = tmp_path / "resume.txt"
    for output in (markdown, html, pdf, plain):
        output.write_text("previous")
    monkeypatch.setattr(renderer, "ensure_command", lambda name: name)

    def run(args, **kwargs):
        if args[0] == "pdftotext":
            return subprocess.CompletedProcess(args, 0, "\f\n", "")
        destination = Path(args[args.index("-o") + 1]) if "-o" in args else Path(args[-1])
        destination.write_text("new output")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(renderer.subprocess, "run", run)
    outputs = (markdown, html, pdf, plain) if kind == "bundle" else (html, pdf)
    with pytest.raises(ValueError, match="PDF"):
        if kind == "bundle":
            renderer.render_markdown_bundle(
                "new markdown",
                markdown_path=markdown,
                html_path=html,
                pdf_path=pdf,
                css_filename="style.css",
                title="Resume",
                plain_text_path=plain,
                css_path=tmp_path / "style.css",
            )
        else:
            renderer.render_pdf_markdown(
                "new markdown",
                html_path=html,
                pdf_path=pdf,
                css_filename="style.css",
                title="Resume",
            )

    assert all(output.read_text() == "previous" for output in outputs)


def test_bundle_stages_each_output_on_destination_filesystem(tmp_path, monkeypatch):
    markdown = tmp_path / "markdown" / "resume.md"
    html = tmp_path / "html" / "resume.html"
    pdf = tmp_path / "pdf" / "resume.pdf"
    plain = tmp_path / "plain" / "resume.txt"
    monkeypatch.setattr(renderer, "ensure_command", lambda name: name)

    def run(args, **kwargs):
        if args[0] == "pdftotext":
            return subprocess.CompletedProcess(args, 0, "Safe output", "")
        destination = Path(args[args.index("-o") + 1]) if "-o" in args else Path(args[-1])
        destination.write_text("Safe output")
        return subprocess.CompletedProcess(args, 0, "", "")

    original_replace = Path.replace

    def replace(source, target):
        source_root = source.relative_to(tmp_path).parts[0]
        target_root = Path(target).relative_to(tmp_path).parts[0]
        if source_root != target_root:
            raise OSError(errno.EXDEV, "cross-device link")
        return original_replace(source, target)

    monkeypatch.setattr(renderer.subprocess, "run", run)
    monkeypatch.setattr(Path, "replace", replace)
    renderer.render_markdown_bundle(
        "Safe markdown",
        markdown_path=markdown,
        html_path=html,
        pdf_path=pdf,
        css_filename="style.css",
        title="Resume",
        plain_text_path=plain,
        css_path=tmp_path / "style.css",
    )

    assert markdown.read_text() == "Safe markdown"
    assert all(output.read_text() == "Safe output" for output in (html, pdf, plain))


def test_submission_keeps_ordinary_citations(tmp_path):
    output = tmp_path / "resume.txt"
    content = "Evidence-based development. 검증 테스트를 추가했습니다. [1] https://example.org/research"
    renderer.write_text_output(output, content)
    assert output.read_text() == content


@pytest.mark.parametrize("kind", ["html", "plain", "pdf"])
def test_rendered_contamination_never_replaces_output(tmp_path, monkeypatch, kind):
    source = tmp_path / "source.md"
    source.write_text("Safe body")
    output = tmp_path / f"resume.{kind}"
    output.write_text("previous")
    monkeypatch.setattr(renderer, "ensure_command", lambda name: name)

    def run(args, **kwargs):
        if args[0] == "pdftotext":
            return subprocess.CompletedProcess(args, 0, "evidence-lane3-04", "")
        destination = Path(args[args.index("-o") + 1]) if "-o" in args else Path(args[-1])
        destination.write_text("evidence-lane3-04")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(renderer.subprocess, "run", run)
    with pytest.raises(ValueError, match="submission"):
        if kind == "html":
            renderer.markdown_to_html(
                source,
                output,
                css_path=tmp_path / "style.css",
                title="Resume",
            )
        elif kind == "plain":
            renderer.markdown_to_plain(source, output)
        else:
            renderer.html_to_pdf(source, output)
    assert output.read_text() == "previous"
