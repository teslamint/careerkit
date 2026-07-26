from __future__ import annotations

import json
from pathlib import Path

from careerkit.resume.adapters.document_renderer import theme_css_path
from careerkit.resume.adapters.filesystem import ResumeWorkspaceAdapter
from careerkit.resume.application.build import ResumeBuildService
from careerkit.resume.cli import main
from careerkit.resume import cli
from careerkit.resume.adapters.document_renderer import RendererUnavailableError
from careerkit.resume.domain.content import extract_company_info_full


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _workspace(tmp_path: Path) -> Path:
    (tmp_path / ".career-workspace").write_text("1\n", encoding="utf-8")
    return tmp_path


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _seed_example_workspace(root: Path) -> None:
    _write_json(root / "example" / "variant_config.json", {
        "public": {"companies": ["Acme"], "company_detail": {"Acme": "full"}, "include_awards": False, "include_languages": False},
        "job": {"companies": ["Acme"], "company_detail": {"Acme": "full"}, "include_awards": False, "include_languages": False},
    })
    _write(root / "example" / "profile" / "contact.md", "# Contact\n\n- Name: Example User\n- Email: example@example.com\n- Phone: 010-0000-0000\n- GitHub: github.com/example\n")
    _write(root / "example" / "profile" / "summary-public.md", "# Summary\n\nExample summary\n")
    _write(root / "example" / "profile" / "skills-public.md", "# Skills\n\n- Python\n")
    _write(root / "example" / "profile" / "education.md", "# Education\n\n## Example University\n")
    _write(root / "example" / "companies" / "Acme" / "profile.md", "# Acme\n\n## Overview\n\n- Period: 2021.01 - 2022.12\n- Role: Backend Engineer\n")
    _write(
        root / "example" / "companies" / "Acme" / "projects" / "api.md",
        "## API Platform\n\n## Tech Stack\n\n- Python\n\nBuilt APIs\n",
    )


def test_theme_resources_resolve() -> None:
    assert theme_css_path("style.css").read_text(encoding="utf-8").startswith("@page")
    assert theme_css_path("style-short.css").exists()
    assert theme_css_path("style-career.css").exists()


def test_cli_build_example_full_writes_expected_outputs(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    _seed_example_workspace(root)
    code = main(["--workspace", str(root), "build", "example", "full"])
    assert code == 0
    build_dir = root / "example" / "build"
    assert (build_dir / "resume-example.md").exists()
    assert (build_dir / "resume-example.html").exists()
    assert (build_dir / "resume-example.pdf").exists()
    assert (build_dir / "resume-example-remember.txt").exists()


def test_cli_example_full_markdown_matches_service_output(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    _seed_example_workspace(root)
    code = main(["--workspace", str(root), "build", "example", "full"])
    assert code == 0
    service = ResumeBuildService(ResumeWorkspaceAdapter(base_dir=root / "example"))
    expected = service.build_full("public")
    actual = (root / "example" / "build" / "resume-example.md").read_text(encoding="utf-8")
    assert actual == expected


def test_cli_validate_example_passes(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    _seed_example_workspace(root)
    code = main(["--workspace", str(root), "validate", "--example"])
    assert code == 0


def test_cli_verbose_flag_accepted(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    _seed_example_workspace(root)
    code = main(["-v", "--workspace", str(root), "validate", "--example"])
    assert code == 0


def test_cli_invalid_target_returns_controlled_error(capsys) -> None:
    assert main(["build", "job", "full", "--target", "../bad"]) == 2
    assert "invalid target name" in capsys.readouterr().err


def test_cli_verify_content_supports_json_and_missing_files(tmp_path: Path, capsys) -> None:
    root = _workspace(tmp_path)
    _write_json(root / "private" / "verify_content_config.json", {
        "company_aliases": {"AlphaCorp": ["AlphaCorp"]},
        "parent_company_map": {},
        "technology_keywords": ["ExampleDB"],
        "pattern_keywords": [],
    })
    interview = root / "interview.md"
    resume = root / "resume.md"
    interview.write_text("# Interview\n", encoding="utf-8")
    resume.write_text("# Resume\n", encoding="utf-8")

    assert main(["--workspace", str(root), "verify-content", str(interview), "--resume", str(resume), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"results": [], "status": "ok"}
    assert main(["--workspace", str(root), "verify-content", "missing.md"]) == 2
    assert "career-resume:" in capsys.readouterr().err


def test_cli_verify_content_requires_private_config(tmp_path: Path, capsys) -> None:
    root = _workspace(tmp_path)
    interview = root / "interview.md"
    resume = root / "resume.md"
    interview.write_text("# Interview\n", encoding="utf-8")
    resume.write_text("# Resume\n", encoding="utf-8")

    assert main(["--workspace", str(root), "verify-content", str(interview), "--resume", str(resume)]) == 2
    assert "verify_content_config.example.json" in capsys.readouterr().err


def test_cli_renderer_error_is_controlled(monkeypatch, capsys) -> None:
    def fail_build(args):
        raise RendererUnavailableError("required renderer command is not installed: pandoc")

    monkeypatch.setattr(cli, "_handle_build", fail_build)
    assert main(["build", "example", "full"]) == 2
    assert "required renderer command" in capsys.readouterr().err


def test_target_full_build_uses_override_css_and_job_pdf_layout(tmp_path: Path, monkeypatch) -> None:
    root = _workspace(tmp_path)
    base = root / "private"
    _write_json(base / "variant_config.json", {"job": {"companies": [], "include_awards": False, "include_languages": False, "include_open_source": False}})
    _write(base / "profile" / "contact.md", "# Contact\n\n- Name: User\n- Email: user@example.com\n")
    _write(base / "profile" / "summary-job.md", "# Summary\n\nJob summary\n")
    _write(base / "profile" / "skills-job.md", "# Skills\n\n- Python\n")
    _write(base / "profile" / "education.md", "# Education\n")
    override_css = base / "overrides" / "acme" / "style.css"
    _write(override_css, "body { color: purple; }\n")
    captured = {}

    def fake_render(markdown_content, **kwargs):
        captured["markdown"] = markdown_content
        captured.update(kwargs)

    monkeypatch.setattr(cli, "render_markdown_bundle", fake_render)

    assert main(["--workspace", str(root), "build", "job", "full", "--target", "acme"]) == 0
    assert captured["css_path"] == override_css
    assert captured["render_markdown_content"].startswith("# Summary")
    assert "# Contact" in captured["markdown"]
    assert "# Contact" not in captured["render_markdown_content"]


def test_targeted_base_build_ignores_target_overrides(tmp_path: Path, monkeypatch) -> None:
    root = _workspace(tmp_path)
    base = root / "private"
    _write_json(
        base / "variant_config.json",
        {
            "job": {
                "companies": [],
                "include_awards": False,
                "include_languages": False,
                "include_open_source": False,
            }
        },
    )
    _write(base / "profile/contact.md", "# Contact\n\n- Name: User\n- Email: user@example.com\n")
    _write(base / "profile/summary-job.md", "# Summary\n\nNeutral summary\n")
    _write(base / "profile/skills-job.md", "# Skills\n\n- Python\n")
    _write(base / "profile/education.md", "# Education\n")
    _write(base / "overrides/acme/profile/summary-job.md", "# Summary\n\nTarget summary\n")
    captured: dict[str, object] = {}

    def fake_render(markdown_content, **kwargs):
        captured["markdown"] = markdown_content

    monkeypatch.setattr(cli, "render_markdown_bundle", fake_render)

    assert main(["--workspace", str(root), "build", "job", "base", "--target", "acme"]) == 0
    assert "Neutral summary" in str(captured["markdown"])
    assert "Target summary" not in str(captured["markdown"])


def test_company_info_defaults_missing_employment_to_full_time() -> None:
    info = extract_company_info_full(
        "# Acme\n\n## Overview\n- Period: 2020.01 - 2022.01\n- Role: Engineer\n"
    )

    assert info["employment"] == "정규직"


def test_job_packet_uses_distinct_default_and_target_outputs(tmp_path: Path, monkeypatch) -> None:
    root = _workspace(tmp_path)
    base = root / "private"
    _write_json(
        base / "variant_config.json",
        {"job": {"companies": ["Acme"], "company_detail": {"Acme": "summary"}}},
    )
    _write(base / "profile/contact.md", "# Contact\n- Name: User\n")
    _write(base / "profile/summary-job.md", "# Summary\n\nBase summary\n")
    _write(base / "profile/skills-job.md", "# Skills\n\n- Python\n")
    _write(
        base / "companies/Acme/profile.md",
        "# Acme\n\n## Overview\n- Period: 2020.01 - 2022.01\n- Role: Engineer\n\nBase career\n",
    )
    _write_json(
        base / "overrides/target-one/config.json",
        {"job": {"companies": ["Acme"], "company_detail": {"Acme": "summary"}}},
    )
    _write(base / "overrides/target-one/profile/summary-job.md", "# Summary\n\nTarget summary\n")
    _write(
        base / "overrides/target-one/companies/Acme/profile.md",
        "# Acme\n\n## Overview\n- Period: 2020.01 - 2022.01\n- Role: Engineer\n\nTarget career\n",
    )
    outputs: dict[str, str] = {}

    def fake_write(path: Path, content: str) -> None:
        outputs[path.name] = content

    def fake_pdf(content: str, **kwargs) -> None:
        outputs[kwargs["pdf_path"].name] = content

    def fake_bundle(content: str, **kwargs) -> None:
        outputs[kwargs["markdown_path"].name] = content
        outputs[kwargs["pdf_path"].name] = kwargs["render_markdown_content"]

    monkeypatch.setattr(cli, "write_text_output", fake_write)
    monkeypatch.setattr(cli, "render_pdf_markdown", fake_pdf)
    monkeypatch.setattr(cli, "render_markdown_bundle", fake_bundle)

    assert main(["--workspace", str(root), "build", "job", "packet"]) == 0
    assert main(["--workspace", str(root), "build", "job", "packet", "--target", "target-one"]) == 0

    assert "Base summary" in outputs["resume-job-short.md"]
    assert "Target summary" in outputs["resume-job-target-one-short.md"]
    assert "Base summary" in outputs["resume-job-short.pdf"]
    assert "Target summary" in outputs["resume-job-target-one-short.pdf"]
    assert "Base career" in outputs["career-description.md"]
    assert "Target career" in outputs["career-description-target-one.md"]
    assert "Base career" in outputs["career-description.pdf"]
    assert "Target career" in outputs["career-description-target-one.pdf"]
    assert "Target" not in outputs["resume-job-short.md"]
    assert "Target" not in outputs["resume-job-short.pdf"]
    assert "Target" not in outputs["career-description.md"]
    assert "Target" not in outputs["career-description.pdf"]


def test_example_packet_keeps_existing_output_names(tmp_path: Path, monkeypatch) -> None:
    root = _workspace(tmp_path)
    _seed_example_workspace(root)
    output_names: set[str] = set()

    def fake_write(path: Path, content: str) -> None:
        output_names.add(path.name)

    def fake_pdf(content: str, **kwargs) -> None:
        output_names.add(kwargs["pdf_path"].name)

    def fake_bundle(content: str, **kwargs) -> None:
        output_names.add(kwargs["markdown_path"].name)
        output_names.add(kwargs["pdf_path"].name)

    monkeypatch.setattr(cli, "write_text_output", fake_write)
    monkeypatch.setattr(cli, "render_pdf_markdown", fake_pdf)
    monkeypatch.setattr(cli, "render_markdown_bundle", fake_bundle)

    assert main(["--workspace", str(root), "build", "example", "packet"]) == 0

    assert {
        "resume-example-short.md",
        "resume-example-short.pdf",
        "career-description-example.md",
        "career-description-example.pdf",
    } <= output_names


def test_career_bundle_uses_pdf_specific_separator_free_content(tmp_path: Path, monkeypatch) -> None:
    base = tmp_path / "private"
    build_dir = tmp_path / "build"
    _write_json(
        base / "variant_config.json",
        {"job": {"companies": ["Acme"], "company_detail": {"Acme": "full"}}},
    )
    _write(base / "companies" / "Acme" / "profile.md", "# Acme\n\n## Overview\n- Period: 2020.01 - 2022.01\n- Role: Engineer\n")
    captured: dict[str, object] = {}

    def fake_render(markdown_content, **kwargs):
        captured["markdown"] = markdown_content
        captured.update(kwargs)

    monkeypatch.setattr(cli, "render_markdown_bundle", fake_render)

    service = ResumeBuildService(ResumeWorkspaceAdapter(base_dir=base))
    cli._render_career_bundle(service, "job", build_dir / "career-description")

    assert "\n\n---\n\n" in str(captured["markdown"])
    assert "\n\n---\n\n" not in str(captured["render_markdown_content"])


def test_validate_reports_missing_schema_fields(tmp_path: Path, capsys) -> None:
    root = _workspace(tmp_path)
    _write(root / "private/profile/contact.md", "# Contact\n- Name: User\n")
    _write(root / "private/companies/Acme/profile.md", "# Acme\n- Period: invalid\n")
    _write(root / "private/companies/Acme/projects/api.md", "# API\n")

    assert main(["--workspace", str(root), "validate"]) == 1

    stderr = capsys.readouterr().err
    assert "Missing required field: Email" in stderr
    assert "Missing required field: Role" in stderr
    assert "Invalid Period format" in stderr
    assert "Section 'Tech Stack' requires at least 1 item" in stderr


def test_validate_rejects_case_only_company_key(tmp_path: Path, capsys) -> None:
    root = _workspace(tmp_path)
    base = root / "private"
    _write_json(base / "variant_config.json", {"job": {"companies": ["acme"]}})
    _write(
        base / "companies/Acme/profile.md",
        "# Acme\n\n## Overview\n- Period: 2020.01 - 2022.01\n- Role: Engineer\n",
    )

    assert main(["--workspace", str(root), "validate"]) == 1

    assert "Company key case mismatch: 'acme' must match directory 'Acme'" in capsys.readouterr().err


def test_validate_accepts_exact_company_key_case(tmp_path: Path, capsys) -> None:
    root = _workspace(tmp_path)
    base = root / "private"
    _write_json(base / "variant_config.json", {"job": {"companies": ["Acme"]}})
    _write(
        base / "companies/Acme/profile.md",
        "# Acme\n\n## Overview\n- Period: 2020.01 - 2022.01\n- Role: Engineer\n",
    )

    assert main(["--workspace", str(root), "validate"]) == 0

    assert capsys.readouterr().out == "All validations passed.\n"


def test_validate_malformed_variant_config_keeps_markdown_validation(tmp_path: Path, capsys) -> None:
    root = _workspace(tmp_path)
    base = root / "private"
    _write(base / "variant_config.json", "{invalid")
    _write(base / "companies/Acme/profile.md", "# Acme\n- Period: invalid\n")

    assert main(["--workspace", str(root), "validate"]) == 1

    assert "Missing required field: Role" in capsys.readouterr().err


def test_validate_non_object_variant_config_keeps_markdown_validation(tmp_path: Path, capsys) -> None:
    root = _workspace(tmp_path)
    base = root / "private"
    _write(base / "variant_config.json", "[]")
    _write(base / "companies/Acme/profile.md", "# Acme\n- Period: invalid\n")

    assert main(["--workspace", str(root), "validate"]) == 1

    assert "Missing required field: Role" in capsys.readouterr().err


def test_validate_rejects_case_only_target_company_key(tmp_path: Path, capsys) -> None:
    root = _workspace(tmp_path)
    base = root / "private"
    _write_json(base / "variant_config.json", {"job": {"companies": ["Acme"]}})
    _write_json(base / "overrides/target-one/config.json", {"job": {"companies": ["acme"]}})
    _write(
        base / "companies/Acme/profile.md",
        "# Acme\n\n## Overview\n- Period: 2020.01 - 2022.01\n- Role: Engineer\n",
    )

    assert main(["--workspace", str(root), "validate"]) == 1

    assert "Company key case mismatch: 'acme' must match directory 'Acme'" in capsys.readouterr().err


def test_validate_rejects_missing_target_company_key(tmp_path: Path, capsys) -> None:
    root = _workspace(tmp_path)
    base = root / "private"
    _write_json(base / "variant_config.json", {"job": {"companies": ["Acme"]}})
    _write_json(base / "overrides/target-one/config.json", {"job": {"companies": ["Missing"]}})
    _write(
        base / "companies/Acme/profile.md",
        "# Acme\n\n## Overview\n- Period: 2020.01 - 2022.01\n- Role: Engineer\n",
    )

    assert main(["--workspace", str(root), "validate"]) == 1

    assert "Company key has no matching directory: 'Missing'" in capsys.readouterr().err


def test_validate_rejects_case_only_company_detail_key(tmp_path: Path, capsys) -> None:
    root = _workspace(tmp_path)
    base = root / "private"
    _write_json(
        base / "variant_config.json",
        {"job": {"companies": ["Acme"], "company_detail": {"acme": "summary"}}},
    )
    _write(
        base / "companies/Acme/profile.md",
        "# Acme\n\n## Overview\n- Period: 2020.01 - 2022.01\n- Role: Engineer\n",
    )

    assert main(["--workspace", str(root), "validate"]) == 1

    assert "Company key case mismatch: 'acme' must match directory 'Acme'" in capsys.readouterr().err


def test_validate_rejects_case_only_target_company_detail_key(tmp_path: Path, capsys) -> None:
    root = _workspace(tmp_path)
    base = root / "private"
    _write_json(
        base / "variant_config.json",
        {"job": {"companies": ["Acme"], "company_detail": {"Acme": "full"}}},
    )
    _write_json(
        base / "overrides/target-one/config.json",
        {"job": {"companies": ["Acme"], "company_detail": {"acme": "summary"}}},
    )
    _write(
        base / "companies/Acme/profile.md",
        "# Acme\n\n## Overview\n- Period: 2020.01 - 2022.01\n- Role: Engineer\n",
    )

    assert main(["--workspace", str(root), "validate"]) == 1

    assert "Company key case mismatch: 'acme' must match directory 'Acme'" in capsys.readouterr().err
