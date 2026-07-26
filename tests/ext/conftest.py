"""Pytest collector for `.cjs` tests under tests/ext/.

Discovers `test_*.cjs` files and runs each via `node <file>` in a subprocess.
Skips gracefully when `node` is not on PATH.
"""

import shutil
import subprocess

import pytest


class CJSTestFailure(Exception):
    def __init__(self, stdout, stderr, returncode):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        super().__init__(
            f"node exited with code {returncode}\n"
            f"--- stdout ---\n{stdout}\n"
            f"--- stderr ---\n{stderr}"
        )


class CJSItem(pytest.Item):
    def runtest(self):
        node = shutil.which("node")
        if not node:
            pytest.skip("node not found on PATH")

        result = subprocess.run(
            [node, str(self.path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise CJSTestFailure(result.stdout, result.stderr, result.returncode)

    def repr_failure(self, excinfo):
        if isinstance(excinfo.value, CJSTestFailure):
            return str(excinfo.value)
        return super().repr_failure(excinfo)

    def reportinfo(self):
        return self.path, 0, f"cjs: {self.name}"


class CJSFile(pytest.File):
    def collect(self):
        yield CJSItem.from_parent(self, name=self.path.name)


def pytest_collect_file(parent, file_path):
    if file_path.suffix == ".cjs" and file_path.name.startswith("test_"):
        return CJSFile.from_parent(parent, path=file_path)
