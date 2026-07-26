#!/usr/bin/env python3
"""Installs the careerkit Native Messaging host for Chrome.

Generates a wrapper script that pins CAREER_WORKSPACE and invokes the host
via `uv run`, then registers a Chrome Native Messaging host manifest
pointing at that wrapper. Generated artifacts live under
`ext/native-host/.generated/` (gitignored) so the committed manifest
template is never overwritten.

Run with: uv run python ext/native-host/install.py
"""

from __future__ import annotations

import os
import shlex
import shutil
import stat
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = SCRIPT_DIR.parents[1]
GENERATED_DIR = SCRIPT_DIR / ".generated"
HOST_SCRIPT = SCRIPT_DIR / "careerkit_host.py"


def _chrome_hosts_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Google" / "Chrome" / "NativeMessagingHosts"
    if sys.platform.startswith("linux"):
        # Chromium (~/.config/chromium/) is not supported — known limitation.
        return Path.home() / ".config" / "google-chrome" / "NativeMessagingHosts"
    print(
        f"error: unsupported platform {sys.platform!r} — only macOS (darwin) and Linux are supported",
        file=sys.stderr,
    )
    raise SystemExit(1)


CHROME_HOSTS_DIR = _chrome_hosts_dir()


def main() -> None:
    wrapper_path = GENERATED_DIR / "careerkit_host_wrapper"
    manifest_path = GENERATED_DIR / "com.careerkit.host.json"

    if not (WORKSPACE_ROOT / ".career-workspace").is_file():
        print(
            f"error: workspace root not found — expected {WORKSPACE_ROOT / '.career-workspace'}",
            file=sys.stderr,
        )
        raise SystemExit(1)

    uv_path = shutil.which("uv")
    if not uv_path:
        print("error: uv not found on PATH — install uv (https://docs.astral.sh/uv/) first", file=sys.stderr)
        raise SystemExit(1)

    GENERATED_DIR.mkdir(parents=True, exist_ok=True)

    wrapper_contents = (
        "#!/bin/bash\n"
        f"export CAREER_WORKSPACE={shlex.quote(str(WORKSPACE_ROOT))}\n"
        f"exec {shlex.quote(uv_path)} run --directory {shlex.quote(str(WORKSPACE_ROOT))} "
        f"python {shlex.quote(str(HOST_SCRIPT))}\n"
    )
    wrapper_path.write_text(wrapper_contents, encoding="utf-8")
    wrapper_path.chmod(wrapper_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    manifest_contents = (
        "{\n"
        '  "name": "com.careerkit.host",\n'
        '  "description": "careerkit Native Messaging host",\n'
        f'  "path": "{wrapper_path}",\n'
        '  "type": "stdio",\n'
        '  "allowed_origins": [\n'
        '    "chrome-extension://EXTENSION_ID_HERE/"\n'
        "  ]\n"
        "}\n"
    )
    manifest_path.write_text(manifest_contents, encoding="utf-8")

    CHROME_HOSTS_DIR.mkdir(parents=True, exist_ok=True)
    symlink_target = CHROME_HOSTS_DIR / "com.careerkit.host.json"
    tmp_path = symlink_target.with_suffix(".tmp")
    tmp_path.unlink(missing_ok=True)
    tmp_path.symlink_to(manifest_path)
    os.replace(str(tmp_path), str(symlink_target))

    print(f"Installed wrapper: {wrapper_path}")
    print(f"Installed manifest: {symlink_target}")
    print(f"Edit allowed_origins in {manifest_path} with your extension ID, then reload.")


if __name__ == "__main__":
    main()
