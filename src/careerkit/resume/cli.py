from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from careerkit.resume.adapters.document_renderer import RendererUnavailableError, render_markdown_bundle, render_pdf_markdown, write_text_output
from careerkit.resume.adapters.filesystem import ResumeWorkspaceAdapter
from careerkit.resume.application.build import ResumeBuildService
from careerkit.resume.application.career_description import build_career
from careerkit.resume.application.headhunter import analyze_template
from careerkit.resume.application.notes import write_notes
from careerkit.resume.application.verify_content import extract_claims, parse_resume_sections, verifier_config_from_data, verify_claims
from careerkit.resume.domain.schema import validate_all
from careerkit.workspace import WorkspaceResolutionError, resolve_workspace
from careerkit.cli_logging import configure_cli_logging

TARGET_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="career-resume")
    parser.add_argument("--workspace", help="Workspace root override")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="Increase logging verbosity (default INFO, -v DEBUG)")
    subparsers = parser.add_subparsers(dest="command")

    build = subparsers.add_parser("build", help="Build resume artifacts")
    build.add_argument("variant", choices=["public", "job", "example"])
    build.add_argument("format", nargs="?", default="all", choices=["full", "short", "wanted", "career", "packet", "base", "all"])
    build.add_argument("--target")
    build.add_argument("--clean", action="store_true")
    build.set_defaults(handler=_handle_build)

    validate = subparsers.add_parser("validate", help="Validate resume sources")
    validate.add_argument("--example", action="store_true")
    validate.set_defaults(handler=_handle_validate)

    verify_content = subparsers.add_parser("verify-content", help="Validate generated content against source evidence")
    verify_content.add_argument("interview")
    verify_content.add_argument("--resume")
    verify_content.add_argument("--config")
    verify_content.add_argument("--json", action="store_true")
    verify_content.set_defaults(handler=_handle_verify_content)

    notes = subparsers.add_parser("notes", help="Generate resume notes")
    notes.add_argument("--base", required=True)
    notes.add_argument("--current", required=True)
    notes.add_argument("--target", default="TBD")
    notes.add_argument("--output", required=True)
    notes.add_argument("--clean", action="store_true")
    notes.set_defaults(handler=_handle_notes)

    headhunter = subparsers.add_parser("headhunter", help="HeadHunter helpers")
    headhunter_subparsers = headhunter.add_subparsers(dest="headhunter_command")
    analyze = headhunter_subparsers.add_parser("analyze", help="Analyze a docx template")
    analyze.add_argument("template")
    analyze.set_defaults(handler=_handle_headhunter_analyze)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    configure_cli_logging(verbose=args.verbose, stream=sys.stderr)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help(sys.stdout)
        return 0
    try:
        return handler(args)
    except (FileNotFoundError, RendererUnavailableError, WorkspaceResolutionError, ValueError) as exc:
        print(f"career-resume: {exc}", file=sys.stderr)
        return 2


def _resolve_base_and_variant(args: argparse.Namespace) -> tuple[Path, str, bool]:
    resolved = resolve_workspace(explicit=args.workspace)
    is_example = args.variant == "example"
    return (resolved.root / "example" if is_example else resolved.root / "private"), ("public" if is_example else args.variant), is_example


def _target_suffix(target: str | None) -> str:
    return f"-{target}" if target else ""


def _ensure_valid_target(target: str | None) -> None:
    if target and not TARGET_PATTERN.match(target):
        raise ValueError("invalid target name; only alphanumeric, hyphen, underscore")


def _build_output_prefix(build_dir: Path, *, variant: str, suffix: str, is_example: bool) -> Path:
    name = f"resume-example{suffix}" if is_example else f"resume-{variant}{suffix}"
    return build_dir / name


def _short_output_prefix(build_dir: Path, *, variant: str, suffix: str, is_example: bool) -> Path:
    name = "resume-example-short" if is_example else f"resume-{variant}{suffix}-short"
    return build_dir / name


def _career_output_prefix(build_dir: Path, *, suffix: str, is_example: bool) -> Path:
    name = "career-description-example" if is_example else f"career-description{suffix}"
    return build_dir / name


def _handle_build(args: argparse.Namespace) -> int:
    _ensure_valid_target(args.target)
    base_dir, variant, is_example = _resolve_base_and_variant(args)
    adapter = ResumeWorkspaceAdapter(base_dir=base_dir, target=args.target)
    service = ResumeBuildService(adapter)
    build_dir = base_dir / "build"
    build_dir.mkdir(parents=True, exist_ok=True)
    suffix = _target_suffix(args.target)
    short_prefix = _short_output_prefix(build_dir, variant=variant, suffix=suffix, is_example=is_example)
    career_prefix = _career_output_prefix(build_dir, suffix=suffix, is_example=is_example)

    if args.format == "base":
        if variant != "job":
            print("career-resume: 'base' format is only available for 'job' variant.", file=sys.stderr)
            return 2
        prefix = _build_output_prefix(build_dir, variant=variant, suffix="-base", is_example=False)
        base_service = ResumeBuildService(ResumeWorkspaceAdapter(base_dir=base_dir))
        _render_full_bundle(base_service, variant, prefix)
        return 0

    if args.format == "full":
        prefix = _build_output_prefix(build_dir, variant=variant, suffix=suffix, is_example=is_example)
        _render_full_bundle(service, variant, prefix)
        if variant == "job" and not is_example:
            _maybe_generate_default_notes(build_dir, target=args.target or "TBD", clean=args.clean, suffix=suffix)
        return 0

    if args.format == "short":
        _render_short_bundle(service, variant, short_prefix)
        return 0

    if args.format == "wanted":
        output_path = build_dir / ("resume-example-wanted.txt" if is_example else f"resume-{variant}-wanted.txt")
        write_text_output(output_path, service.build_wanted(variant))
        return 0

    if args.format == "career":
        _render_career_bundle(service, variant, career_prefix)
        return 0

    if args.format == "packet":
        _render_short_bundle(service, variant, short_prefix)
        _render_career_bundle(service, variant, career_prefix)
        return 0

    if args.format == "all":
        full_prefix = _build_output_prefix(build_dir, variant=variant, suffix=suffix, is_example=is_example)
        wanted_output = build_dir / ("resume-example-wanted.txt" if is_example else f"resume-{variant}-wanted.txt")
        _render_full_bundle(service, variant, full_prefix)
        _render_short_bundle(service, variant, short_prefix)
        write_text_output(wanted_output, service.build_wanted(variant))
        if variant == "job" and not is_example:
            _maybe_generate_default_notes(build_dir, target=args.target or "TBD", clean=args.clean, suffix=suffix)
        return 0

    print(f"career-resume: unsupported format {args.format}", file=sys.stderr)
    return 2


def _render_full_bundle(service: ResumeBuildService, variant: str, prefix: Path) -> None:
    markdown_content = service.build_full(variant)
    target_css = None
    if service.adapter.target:
        candidate = service.adapter.overrides_dir / service.adapter.target / "style.css"
        if candidate.is_file():
            target_css = candidate
    render_markdown_bundle(
        markdown_content,
        markdown_path=prefix.with_suffix(".md"),
        html_path=prefix.with_suffix(".html"),
        pdf_path=prefix.with_suffix(".pdf"),
        css_filename="style.css",
        plain_text_path=prefix.with_name(prefix.name + "-remember.txt"),
        render_markdown_content=service.build_full_pdf(variant),
        css_path=target_css,
    )


def _render_short_bundle(service: ResumeBuildService, variant: str, prefix: Path) -> None:
    markdown_content = service.build_short(variant)
    write_text_output(prefix.with_suffix(".md"), markdown_content)
    render_pdf_markdown(service.build_short_pdf(variant), html_path=prefix.with_suffix(".html"), pdf_path=prefix.with_suffix(".pdf"), css_filename="style-short.css")


def _render_career_bundle(service: ResumeBuildService, variant: str, prefix: Path) -> None:
    render_markdown_bundle(
        build_career(service.adapter, variant),
        markdown_path=prefix.with_suffix(".md"),
        html_path=prefix.with_suffix(".html"),
        pdf_path=prefix.with_suffix(".pdf"),
        css_filename="style-career.css",
        render_markdown_content=build_career(service.adapter, variant, format_type="pdf"),
    )


def _maybe_generate_default_notes(build_dir: Path, *, target: str, clean: bool, suffix: str) -> None:
    base_path = build_dir / "resume-job-base.md"
    current_path = build_dir / f"resume-job{suffix}.md"
    output_path = build_dir / "resume-job-notes.md"
    if base_path.exists() and current_path.exists():
        write_notes(base_path, current_path, output_path, target=target, clean=clean)


def _handle_validate(args: argparse.Namespace) -> int:
    resolved = resolve_workspace(explicit=args.workspace)
    base_dir = resolved.root / ("example" if args.example else "private")
    errors = validate_all(base_dir)
    if errors:
        for error in errors:
            print(f"{error.file_path}:{error.line or 0}: {error.message}", file=sys.stderr)
        return 1
    print("All validations passed.")
    return 0


def _handle_verify_content(args: argparse.Namespace) -> int:
    resolved = resolve_workspace(explicit=args.workspace)
    adapter = ResumeWorkspaceAdapter(base_dir=resolved.root / "private", workspace=resolved)
    config_path = Path(args.config) if args.config else adapter.verify_content_config_path
    if not config_path.is_absolute():
        config_path = resolved.root / config_path
    config = verifier_config_from_data(adapter.load_verify_content_config(config_path))
    interview_path = Path(args.interview)
    resume_path = Path(args.resume) if args.resume else resolved.root / "private" / "build" / "resume-job-base.md"
    sections = parse_resume_sections(resume_path, config)
    claims = extract_claims(interview_path, config)
    if not claims:
        if args.json:
            print(json.dumps({"results": [], "status": "ok"}, sort_keys=True))
            return 0
        print("No company-specific claims found in interview sheet.")
        return 0
    results = verify_claims(claims, sections, config)
    if args.json:
        print(json.dumps({
            "status": "failed" if any(item.status == "ungrounded" for item in results) else "ok",
            "results": [
                {
                    "status": item.status,
                    "company_key": item.claim.company_key,
                    "keyword": item.claim.keyword,
                }
                for item in results
            ],
        }, ensure_ascii=False, sort_keys=True))
        return 1 if any(item.status == "ungrounded" for item in results) else 0
    for result in results:
        print(f"{result.status}: [{result.claim.company_key}] {result.claim.keyword}")
    return 1 if any(result.status == "ungrounded" for result in results) else 0


def _handle_notes(args: argparse.Namespace) -> int:
    write_notes(Path(args.base), Path(args.current), Path(args.output), target=args.target, clean=args.clean)
    return 0


def _handle_headhunter_analyze(args: argparse.Namespace) -> int:
    print(analyze_template(args.template))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
