from __future__ import annotations

import json
import io
import logging
from pathlib import Path

import pytest

from careerkit.jobs.cli import build_parser, main


@pytest.fixture(autouse=True)
def _restore_root_logging():
    root = logging.getLogger()
    old_level = root.level
    old_handlers = list(root.handlers)
    yield
    root.setLevel(old_level)
    root.handlers[:] = old_handlers


def _workspace(tmp_path: Path) -> Path:
    (tmp_path / ".career-workspace").write_text("1\n", encoding="utf-8")
    return tmp_path


class TestVerboseFlag:
    def test_default_verbose_is_zero(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["config", "check"])
        assert args.verbose == 0

    def test_single_v_sets_one(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["-v", "config", "check"])
        assert args.verbose == 1

    def test_double_v_sets_two(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["-vv", "config", "check"])
        assert args.verbose == 2

    def test_long_flag_sets_one(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["--verbose", "config", "check"])
        assert args.verbose == 1


class TestLoggingConfiguration:
    def test_info_level_by_default(self, tmp_path: Path) -> None:
        root = _workspace(tmp_path)
        main(["--workspace", str(root), "config", "check"])
        assert logging.getLogger().level == logging.INFO

    def test_debug_level_with_verbose(self, tmp_path: Path) -> None:
        root = _workspace(tmp_path)
        main(["-v", "--workspace", str(root), "config", "check"])
        assert logging.getLogger().level == logging.DEBUG

    def test_existing_root_handler_is_preserved(self, tmp_path: Path) -> None:
        root = _workspace(tmp_path)
        handler = logging.StreamHandler(io.StringIO())
        logging.getLogger().addHandler(handler)

        main(["--workspace", str(root), "config", "check"])

        assert handler in logging.getLogger().handlers
        assert handler.stream is not None


class TestLoggingOutput:
    def test_no_debug_without_verbose(self, tmp_path: Path, capsys) -> None:
        root = _workspace(tmp_path)
        main(["--workspace", str(root), "config", "check"])
        stderr = capsys.readouterr().err
        assert "DEBUG" not in stderr

    def test_json_stdout_not_corrupted_with_verbose(self, tmp_path: Path, capsys) -> None:
        root = _workspace(tmp_path)
        main(["-v", "--workspace", str(root), "config", "check", "--json"])
        captured = capsys.readouterr()
        parsed = json.loads(captured.out)
        assert "command" in parsed
        assert "DEBUG" not in captured.out

    def test_run_auto_default_logs_info_to_stderr(self, tmp_path: Path, capsys) -> None:
        root = _workspace(tmp_path)
        main([
            "--workspace",
            str(root),
            "run",
            "auto",
            "--dry-run",
            "--from-urls",
            "/dev/null",
            "--json",
        ])
        captured = capsys.readouterr()
        json.loads(captured.out)
        assert "INFO careerkit.jobs.application.automation: auto:" in captured.err
        assert "INFO careerkit.jobs.application.automation: search complete:" in captured.err

    def test_run_auto_verbose_logs_debug_to_stderr(self, tmp_path: Path, capsys) -> None:
        root = _workspace(tmp_path)
        main([
            "-v",
            "--workspace",
            str(root),
            "run",
            "auto",
            "--dry-run",
            "--from-urls",
            "/dev/null",
            "--json",
        ])
        captured = capsys.readouterr()
        json.loads(captured.out)
        assert "DEBUG careerkit.jobs.application.automation: auto args:" in captured.err
