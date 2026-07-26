from __future__ import annotations

from pathlib import Path

from careerkit.jobs.adapters.screening import cli_provider
from careerkit.jobs.console import server as console_server


def test_cli_provider_minimizes_environment_and_redacts_sensitive_errors() -> None:
    env = {
        "PATH": "/bin",
        "HOME": "/tmp/home",
        "ANTHROPIC_API_KEY": "SENTINEL_SECRET_TOKEN",
        "UNRELATED_SECRET": "should-not-pass",
        "CLAUDECODE": "1",
    }
    built = cli_provider.build_provider_env(env)
    assert built["PATH"] == "/bin"
    assert built["ANTHROPIC_API_KEY"] == "SENTINEL_SECRET_TOKEN"
    assert "UNRELATED_SECRET" not in built
    assert "CLAUDECODE" not in built
    message = cli_provider.format_failed_process(
        "claude",
        1,
        "",
        "boom SENTINEL_SECRET_TOKEN",
        redactions=cli_provider.collect_redactions(built),
    )
    assert "SENTINEL_SECRET_TOKEN" not in message
    assert "[redacted]" in message


def test_codex_capture_file_is_private_and_cleaned_up(tmp_path: Path, monkeypatch) -> None:
    created: list[Path] = []
    real_mkstemp = cli_provider.tempfile.mkstemp

    def wrapped_mkstemp(*args, **kwargs):
        fd, path = real_mkstemp(dir=tmp_path, *args, **kwargs)
        created.append(Path(path))
        return fd, path

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, input, text, capture_output, timeout, env):
        output_path = Path(cmd[-1])
        output_path.write_text("captured output", encoding="utf-8")
        return Result()

    monkeypatch.setattr(cli_provider.tempfile, "mkstemp", wrapped_mkstemp)
    monkeypatch.setattr(cli_provider.subprocess, "run", fake_run)

    code, stdout, stderr = cli_provider.run_provider_command(
        "codex",
        ["codex", "exec"],
        "prompt",
        10,
        {"PATH": "/bin"},
    )
    assert code == 0
    assert stdout == "captured output"
    assert stderr == ""
    assert created
    for path in created:
        assert not path.exists()


def test_console_assets_and_screening_prompt_load_without_optional_runtime_tools() -> None:
    html = console_server.resources.files("careerkit.jobs.console.static").joinpath("index.html").read_text(encoding="utf-8")
    prompt = Path(cli_provider.__file__).parents[2] / "resources" / "prompts" / "screening_system.txt"
    assert "JD Console" in html
    assert prompt.read_text(encoding="utf-8").startswith("아래 기준으로 JD 스크리닝을 수행하세요.")
