from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import tempfile
from typing import Any, Protocol
import urllib.request

MAX_FALLBACK_REASON_CHARS = 240
_ENV_ALLOWLIST = {
    "HOME",
    "USER",
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "TMP",
    "TEMP",
    "TERM",
    "COLORTERM",
    "NO_COLOR",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_ORG_ID",
    "OPENAI_PROJECT",
}
_REDACTED = "[redacted]"


class LocalLLMConfigError(RuntimeError):
    pass


class LLMProvider(Protocol):
    def run(
        self,
        prompt: str,
        timeout: int,
        local_timeout: int | None = None,
    ) -> tuple[str, str]:
        """Return the provider name and LLM output."""
        ...


class FakeProvider:
    def __init__(self, output: str, provider_name: str = "fake") -> None:
        self.output = output
        self.provider_name = provider_name

    def run(
        self,
        prompt: str,
        timeout: int,
        local_timeout: int | None = None,
    ) -> tuple[str, str]:
        return self.provider_name, self.output


CONTEXT_WARNING_RATIO = 0.9


class CLIProvider:
    def __init__(self, *, environment: dict[str, str] | None = None) -> None:
        self.environment = dict(os.environ if environment is None else environment)
        # Why a mutable attribute rather than a wider return type: LLMProvider is a
        # Protocol implemented by FakeProvider and several test doubles, so widening
        # `run` would break every one of them. Callers read this with getattr.
        # A list per provider, not one status: a screening can invoke the same
        # provider twice through the structural retry, and a later "ok" replacing an
        # earlier timeout would turn "one attempt failed" into "everything passed".
        self.last_attempts: dict[str, list[str]] = {}
        self.last_context_warning: str | None = None

    def reset_observations(self) -> None:
        """Clear the attempt chain for a new screening.

        The unit is one screening, not one run(): a screening may call run() twice
        through the structural-retry path, and both calls belong to the same chain.
        """
        self.last_attempts = {}
        self.last_context_warning = None

    def _append(self, provider: str, detail: str) -> None:
        self.last_attempts.setdefault(provider, []).append(detail)

    def _record(self, provider: str, detail: str) -> str:
        prefix = f"{provider}: "
        self._append(provider, detail[len(prefix) :] if detail.startswith(prefix) else detail)
        return detail

    def run(
        self,
        prompt: str,
        timeout: int,
        local_timeout: int | None = None,
    ) -> tuple[str, str]:
        env = build_provider_env(self.environment)
        redactions = collect_redactions(env)
        errors: list[str] = []
        resolved_local_timeout = local_timeout if local_timeout is not None else timeout
        # No reset here: the structural-retry path calls run() a second time on this
        # same instance, and clearing would discard the first call's chain — the very
        # failures this telemetry exists to show. run_screening resets per screening.
        for provider, cmd in resolve_commands(self.environment):
            try:
                returncode, stdout, stderr = run_provider_command(
                    provider,
                    cmd,
                    prompt,
                    timeout,
                    env,
                )
            except FileNotFoundError:
                errors.append(self._record(provider, f"{provider}: command not found"))
                continue
            except subprocess.TimeoutExpired:
                errors.append(
                    self._record(provider, f"{provider}: timed out after {timeout}s")
                )
                continue
            except Exception as exc:
                errors.append(
                    self._record(
                        provider,
                        classify_provider_error(provider, str(exc), redactions=redactions),
                    )
                )
                continue

            if returncode != 0:
                errors.append(
                    self._record(
                        provider,
                        format_failed_process(
                            provider,
                            returncode,
                            stdout,
                            stderr,
                            redactions=redactions,
                        ),
                    )
                )
                continue

            output = stdout.strip()
            if not output:
                errors.append(self._record(provider, f"{provider}: returned empty output"))
                continue
            self._append(provider, "ok")
            return provider, output

        try:
            local = resolve_local_llm(self.environment)
        except LocalLLMConfigError as exc:
            errors.append(self._record("ollama", str(exc)))
            local = None
        if local is not None:
            label, url, model, extra_options = local
            if not model:
                errors.append(self._record(label, f"{label}: LOCAL_LLM_MODEL not set"))
            else:
                try:
                    output, prompt_tokens = run_local_llm(
                        label,
                        url,
                        model,
                        extra_options,
                        prompt,
                        resolved_local_timeout,
                    )
                except Exception as exc:
                    errors.append(
                        self._record(
                            label,
                            classify_provider_error(label, str(exc), redactions=redactions),
                        )
                    )
                else:
                    self._append(label, "ok")
                    self.last_context_warning = context_warning(
                        label, prompt_tokens, extra_options.get("num_ctx")
                    )
                    return label, output.strip()

        raise RuntimeError("; ".join(errors) or "No LLM provider succeeded")


def context_warning(label: str, prompt_tokens: int | None, num_ctx: int | None) -> str | None:
    """Warn from the server's own token count, never from a character estimate.

    The OpenAI-compatible path has no num_ctx to compare against — the window is
    the remote server's setting — so it never warns.
    """
    if prompt_tokens is None or not num_ctx:
        return None
    threshold = num_ctx * CONTEXT_WARNING_RATIO
    if prompt_tokens <= threshold:
        return None
    return (
        f"{label}: prompt {prompt_tokens} tokens exceeds "
        f"{int(CONTEXT_WARNING_RATIO * 100)}% of num_ctx {num_ctx}"
    )


def resolve_local_llm(
    environment: dict[str, str],
) -> tuple[str, str, str, dict[str, Any]] | None:
    base_url = environment.get("LOCAL_LLM_BASE_URL")
    if base_url:
        model = environment.get("LOCAL_LLM_MODEL") or ""
        return "local", f"{base_url.rstrip('/')}/chat/completions", model, {}

    ollama_model = environment.get("OLLAMA_SCREENING_MODEL") or "gpt-oss:20b"
    if ollama_model == "off":
        return None
    ollama_base = environment.get("OLLAMA_BASE_URL") or "http://localhost:11434"
    raw_num_ctx = environment.get("OLLAMA_NUM_CTX") or "32768"
    try:
        num_ctx = int(raw_num_ctx)
    except ValueError:
        raise LocalLLMConfigError(f"ollama: invalid OLLAMA_NUM_CTX {raw_num_ctx}") from None
    if num_ctx <= 0:
        raise LocalLLMConfigError(f"ollama: OLLAMA_NUM_CTX must be positive, got {num_ctx}")
    return "ollama", f"{ollama_base.rstrip('/')}/api/chat", ollama_model, {"num_ctx": num_ctx}


def run_local_llm(
    label: str,
    url: str,
    model: str,
    extra_options: dict[str, Any],
    prompt: str,
    timeout: int,
) -> tuple[str, int | None]:
    """Return (content, prompt token count as the server reported it)."""
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }
    if label == "ollama":
        body["options"] = extra_options
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if label == "ollama":
        content = payload.get("message", {}).get("content", "")
        prompt_tokens = payload.get("prompt_eval_count")
    else:
        content = payload["choices"][0]["message"]["content"]
        prompt_tokens = (payload.get("usage") or {}).get("prompt_tokens")
    if not content or not content.strip():
        raise RuntimeError("returned empty output")
    return content, prompt_tokens if isinstance(prompt_tokens, int) else None


def build_provider_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    source = os.environ if base_env is None else base_env
    env = {key: value for key, value in source.items() if key in _ENV_ALLOWLIST and value}
    env.pop("CLAUDECODE", None)
    return env


def collect_redactions(environment: dict[str, str]) -> tuple[str, ...]:
    values: list[str] = []
    for key, value in environment.items():
        if len(value) < 4:
            continue
        if key.endswith("_KEY") or "TOKEN" in key or "SECRET" in key:
            values.append(value)
    return tuple(dict.fromkeys(values))


def redact_text(text: str, *, redactions: tuple[str, ...] = ()) -> str:
    redacted = text
    for value in redactions:
        if value:
            redacted = redacted.replace(value, _REDACTED)
    return redacted


def find_executable(name: str, extra_paths: list[Path] | None = None) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for path in extra_paths or []:
        if path.exists() and os.access(path, os.X_OK):
            return str(path)
    return None


def resolve_commands(environment: dict[str, str] | None = None) -> list[tuple[str, list[str]]]:
    env = os.environ if environment is None else environment
    claude_cmd = env.get("CLAUDE_SCREENING_CMD")
    codex_cmd = env.get("CODEX_SCREENING_CMD")

    claude_model = env.get("CLAUDE_SCREENING_MODEL", "")
    codex_model = env.get("CODEX_SCREENING_MODEL", "")

    if not claude_cmd:
        claude_bin = find_executable(
            "claude",
            [Path.home() / ".local" / "bin" / "claude", Path("/opt/homebrew/bin/claude")],
        )
        model_flag = f" --model {claude_model}" if claude_model else ""
        claude_cmd = f"{claude_bin} --print{model_flag}" if claude_bin else None

    if not codex_cmd:
        codex_bin = find_executable("codex", [Path("/opt/homebrew/bin/codex")])
        model_flag = f" --model {codex_model}" if codex_model else ""
        codex_cmd = f"{codex_bin} exec{model_flag}" if codex_bin else None

    providers: list[tuple[str, list[str]]] = []
    if claude_cmd:
        providers.append(("claude", shlex.split(claude_cmd)))
    if codex_cmd:
        providers.append(("codex", shlex.split(codex_cmd)))
    return providers


def is_codex_exec_command(cmd: list[str]) -> bool:
    return bool(cmd) and Path(cmd[0]).name == "codex" and len(cmd) > 1 and cmd[1] == "exec"


def should_capture_codex_last_message(cmd: list[str]) -> bool:
    return is_codex_exec_command(cmd) and "--output-last-message" not in cmd and "-o" not in cmd


def classify_provider_error(provider: str, detail: str, *, redactions: tuple[str, ...] = ()) -> str:
    text = redact_text(detail.replace("\r", "\n").strip(), redactions=redactions)
    lowered = text.lower()

    if "not logged in" in lowered and "please run /login" in lowered:
        return f"{provider}: not logged in"

    if provider == "codex":
        if "readonly database" in lowered or (
            "operation not permitted" in lowered and ".codex" in lowered
        ):
            return f"{provider}: blocked by Codex App sandbox/home state"
        if "operation not permitted" in lowered and "app-server" in lowered:
            return f"{provider}: blocked by Codex App sandbox"

    if (
        "failed to lookup address information" in lowered
        or "could not resolve host" in lowered
        or "network is unreachable" in lowered
    ):
        return f"{provider}: network unavailable"

    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if not first_line:
        return f"{provider}: execution failed"
    if len(first_line) > MAX_FALLBACK_REASON_CHARS:
        first_line = first_line[:MAX_FALLBACK_REASON_CHARS].rstrip() + "..."
    return f"{provider}: {first_line}"


def format_failed_process(
    provider: str,
    returncode: int,
    stdout: str,
    stderr: str,
    *,
    redactions: tuple[str, ...] = (),
) -> str:
    detail = "\n".join(part for part in (stderr.strip(), stdout.strip()) if part)
    classified = classify_provider_error(provider, detail, redactions=redactions)
    if classified.startswith(f"{provider}:"):
        return classified
    return f"{provider}: exit={returncode}"


def run_provider_command(
    provider: str,
    cmd: list[str],
    prompt: str,
    timeout: int,
    env: dict[str, str],
) -> tuple[int, str, str]:
    output_path: Path | None = None
    run_cmd = list(cmd)

    if should_capture_codex_last_message(run_cmd):
        fd, path = tempfile.mkstemp(prefix="jd-screening-codex-", suffix=".md")
        os.close(fd)
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        output_path = Path(path)
        run_cmd.extend(["--output-last-message", str(output_path)])

    try:
        proc = subprocess.run(
            run_cmd,
            input=prompt,
            text=True,
            capture_output=True,
            timeout=timeout,
            env=env,
        )
        stdout = proc.stdout
        if proc.returncode == 0 and output_path and output_path.exists():
            captured = output_path.read_text(encoding="utf-8").strip()
            if captured:
                stdout = captured
        return proc.returncode, stdout, proc.stderr
    finally:
        if output_path is not None:
            try:
                output_path.unlink()
            except FileNotFoundError:
                pass
