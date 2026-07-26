import json
import subprocess
import urllib.error

import pytest

from careerkit.jobs.adapters.screening import cli_provider
from careerkit.jobs.adapters.screening.cli_provider import (
    CLIProvider,
    resolve_local_llm,
    run_local_llm,
)


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> bool:
        return False


def install_urlopen(monkeypatch: pytest.MonkeyPatch, payload: dict) -> list[dict]:
    calls: list[dict] = []

    def fake_urlopen(request, timeout=None):
        calls.append(
            {
                "url": request.full_url,
                "body": json.loads(request.data.decode("utf-8")),
                "timeout": timeout,
                "content_type": request.get_header("Content-type"),
            }
        )
        return FakeResponse(payload)

    monkeypatch.setattr(cli_provider.urllib.request, "urlopen", fake_urlopen)
    return calls


def forbid_urlopen(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    calls: list[dict] = []

    def fake_urlopen(request, timeout=None):
        calls.append({"url": request.full_url})
        raise AssertionError("urlopen must not be called")

    monkeypatch.setattr(cli_provider.urllib.request, "urlopen", fake_urlopen)
    return calls


@pytest.fixture
def no_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli_provider, "find_executable", lambda name, extra_paths=None: None
    )


def test_resolve_local_llm_defaults() -> None:
    resolved = resolve_local_llm({})
    assert resolved is not None
    label, url, model, extra = resolved
    assert label == "ollama"
    assert url == "http://localhost:11434/api/chat"
    assert model == "gpt-oss:20b"
    assert extra == {"num_ctx": 32768}


def test_resolve_local_llm_strips_trailing_slash() -> None:
    ollama_resolved = resolve_local_llm({"OLLAMA_BASE_URL": "http://box:11434/"})
    assert ollama_resolved is not None
    assert ollama_resolved[1] == "http://box:11434/api/chat"

    local_resolved = resolve_local_llm(
        {"LOCAL_LLM_BASE_URL": "http://box:8080/v1/", "LOCAL_LLM_MODEL": "m"}
    )
    assert local_resolved is not None
    label, url, model, extra = local_resolved
    assert label == "local"
    assert url == "http://box:8080/v1/chat/completions"
    assert model == "m"
    assert extra == {}


def test_resolve_local_llm_off_returns_none() -> None:
    assert resolve_local_llm({"OLLAMA_SCREENING_MODEL": "off"}) is None


def test_run_falls_back_to_ollama(
    monkeypatch: pytest.MonkeyPatch, no_cli: None
) -> None:
    calls = install_urlopen(
        monkeypatch, {"message": {"content": "결과", "thinking": "생각"}}
    )
    provider = CLIProvider(environment={})
    assert provider.run("prompt", timeout=42) == ("ollama", "결과")
    assert calls[0]["url"].endswith("/api/chat")
    assert calls[0]["timeout"] == 42
    assert calls[0]["content_type"] == "application/json"


def test_run_uses_configured_local_timeout(
    monkeypatch: pytest.MonkeyPatch, no_cli: None
) -> None:
    calls = install_urlopen(
        monkeypatch, {"message": {"content": "결과", "thinking": "생각"}}
    )
    provider = CLIProvider(environment={})
    assert provider.run("prompt", timeout=10, local_timeout=30) == ("ollama", "결과")
    assert calls[0]["timeout"] == 30


def test_run_uses_openai_compatible_endpoint(
    monkeypatch: pytest.MonkeyPatch, no_cli: None
) -> None:
    calls = install_urlopen(
        monkeypatch, {"choices": [{"message": {"content": "결과"}}]}
    )
    provider = CLIProvider(
        environment={
            "LOCAL_LLM_BASE_URL": "http://box:8080/v1",
            "LOCAL_LLM_MODEL": "llama",
        }
    )
    assert provider.run("prompt", timeout=10) == ("local", "결과")
    assert calls[0]["url"].endswith("/chat/completions")
    body = calls[0]["body"]
    assert body["model"] == "llama"
    assert body["stream"] is False
    assert "options" not in body


def test_run_skips_local_when_ollama_off(
    monkeypatch: pytest.MonkeyPatch, no_cli: None
) -> None:
    calls = forbid_urlopen(monkeypatch)
    provider = CLIProvider(environment={"OLLAMA_SCREENING_MODEL": "off"})
    with pytest.raises(RuntimeError):
        provider.run("prompt", timeout=5)
    assert calls == []


def test_run_off_still_uses_openai_path_when_base_url_set(
    monkeypatch: pytest.MonkeyPatch, no_cli: None
) -> None:
    calls = install_urlopen(
        monkeypatch, {"choices": [{"message": {"content": "결과"}}]}
    )
    provider = CLIProvider(
        environment={
            "OLLAMA_SCREENING_MODEL": "off",
            "LOCAL_LLM_BASE_URL": "http://box:8080/v1",
            "LOCAL_LLM_MODEL": "llama",
        }
    )
    assert provider.run("prompt", timeout=5) == ("local", "결과")
    assert calls[0]["url"].endswith("/chat/completions")


def test_run_ollama_request_body(
    monkeypatch: pytest.MonkeyPatch, no_cli: None
) -> None:
    calls = install_urlopen(monkeypatch, {"message": {"content": "ok"}})
    provider = CLIProvider(environment={})
    provider.run("질문", timeout=5)
    body = calls[0]["body"]
    assert body["model"] == "gpt-oss:20b"
    assert body["messages"] == [{"role": "user", "content": "질문"}]
    assert body["stream"] is False
    assert body["options"]["num_ctx"] == 32768


def test_run_local_llm_empty_content_raises() -> None:
    with pytest.raises(RuntimeError, match="returned empty output"):
        run_local_llm_with_payload({"message": {"content": "   "}})


def run_local_llm_with_payload(payload: dict) -> tuple[str, int | None]:
    mp = pytest.MonkeyPatch()
    try:
        install_urlopen(mp, payload)
        return run_local_llm(
            "ollama", "http://localhost:11434/api/chat", "m", {}, "p", 5
        )
    finally:
        mp.undo()


def test_run_aggregates_http_error(
    monkeypatch: pytest.MonkeyPatch, no_cli: None
) -> None:
    def failing_urlopen(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(cli_provider.urllib.request, "urlopen", failing_urlopen)
    provider = CLIProvider(environment={})
    with pytest.raises(RuntimeError, match="ollama:"):
        provider.run("prompt", timeout=5)


def test_run_aggregates_empty_content_error(
    monkeypatch: pytest.MonkeyPatch, no_cli: None
) -> None:
    install_urlopen(monkeypatch, {"message": {"content": ""}})
    provider = CLIProvider(environment={})
    with pytest.raises(RuntimeError, match="ollama: returned empty output"):
        provider.run("prompt", timeout=5)


def test_run_reports_invalid_num_ctx_without_calling_ollama(
    monkeypatch: pytest.MonkeyPatch, no_cli: None
) -> None:
    calls = forbid_urlopen(monkeypatch)
    provider = CLIProvider(environment={"OLLAMA_NUM_CTX": "abc"})
    with pytest.raises(RuntimeError, match="ollama: invalid OLLAMA_NUM_CTX abc"):
        provider.run("prompt", timeout=5)
    assert calls == []


def test_run_error_lists_all_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing_urlopen(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(cli_provider.urllib.request, "urlopen", failing_urlopen)
    provider = CLIProvider(
        environment={
            "CLAUDE_SCREENING_CMD": "/usr/bin/false",
            "CODEX_SCREENING_CMD": "/usr/bin/false",
        }
    )
    with pytest.raises(RuntimeError) as excinfo:
        provider.run("prompt", timeout=5)
    message = str(excinfo.value)
    assert "claude" in message
    assert "codex" in message
    assert "ollama" in message


def test_run_reports_missing_local_llm_model(
    monkeypatch: pytest.MonkeyPatch, no_cli: None
) -> None:
    calls = forbid_urlopen(monkeypatch)
    provider = CLIProvider(environment={"LOCAL_LLM_BASE_URL": "http://box:8080/v1"})
    with pytest.raises(RuntimeError, match="local: LOCAL_LLM_MODEL not set"):
        provider.run("prompt", timeout=5)
    assert calls == []


def test_attempts_record_ok_for_the_succeeding_provider(
    monkeypatch: pytest.MonkeyPatch, no_cli: None
) -> None:
    install_urlopen(monkeypatch, {"message": {"content": "ok"}})
    provider = CLIProvider(environment={})

    provider.run("prompt", timeout=5)

    assert provider.last_attempts == {"ollama": ["ok"]}


def test_attempts_preserve_earlier_failures_after_a_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli_provider,
        "resolve_commands",
        lambda environment=None: [("claude", ["claude", "--print"])],
    )

    def missing(provider, cmd, prompt, timeout, env):
        raise FileNotFoundError(provider)

    monkeypatch.setattr(cli_provider, "run_provider_command", missing)
    install_urlopen(monkeypatch, {"message": {"content": "ok"}})
    provider = CLIProvider(environment={})

    label, _ = provider.run("prompt", timeout=5)

    assert label == "ollama"
    assert provider.last_attempts == {"claude": ["command not found"], "ollama": ["ok"]}


def test_reset_observations_clears_the_chain(
    monkeypatch: pytest.MonkeyPatch, no_cli: None
) -> None:
    install_urlopen(monkeypatch, {"message": {"content": "ok"}})
    provider = CLIProvider(environment={})
    provider.last_attempts = {"stale": ["leftover"]}
    provider.last_context_warning = "stale warning"

    provider.reset_observations()
    provider.run("prompt", timeout=5)

    assert "stale" not in provider.last_attempts


def test_attempts_survive_a_second_run_within_one_screening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The structural-retry path calls run() twice on one instance. Clearing per
    run would report only the retry, hiding the attempt that caused the retry."""
    calls: list[int] = []

    def claude_then_gone(provider, cmd, prompt, timeout, env):
        calls.append(1)
        raise FileNotFoundError(provider)

    monkeypatch.setattr(
        cli_provider,
        "resolve_commands",
        lambda environment=None: [("claude", ["claude", "--print"])],
    )
    monkeypatch.setattr(cli_provider, "run_provider_command", claude_then_gone)
    install_urlopen(monkeypatch, {"message": {"content": "ok"}})
    provider = CLIProvider(environment={})
    provider.reset_observations()

    provider.run("prompt", timeout=5)
    provider.run("retry prompt", timeout=5)

    assert len(calls) == 2
    assert provider.last_attempts == {
        "claude": ["command not found", "command not found"],
        "ollama": ["ok", "ok"],
    }


def test_a_later_success_does_not_erase_an_earlier_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One status per provider would report claude as simply ok, turning "an
    attempt failed" into "everything passed" — the reading this telemetry exists
    to prevent."""
    outcomes = iter([TimeoutError, None])

    def flaky(provider, cmd, prompt, timeout, env):
        if next(outcomes) is TimeoutError:
            raise subprocess.TimeoutExpired(cmd, timeout)
        return 0, "결과", ""

    monkeypatch.setattr(
        cli_provider,
        "resolve_commands",
        lambda environment=None: [("claude", ["claude", "--print"])],
    )
    monkeypatch.setattr(cli_provider, "run_provider_command", flaky)
    forbid_urlopen(monkeypatch)
    # Local path off, so the chain is claude alone and the assertion below is about
    # claude's own two attempts rather than a fallback that happened in between.
    provider = CLIProvider(environment={"OLLAMA_SCREENING_MODEL": "off"})
    provider.reset_observations()

    with pytest.raises(RuntimeError):
        provider.run("prompt", timeout=5)
    assert provider.run("retry prompt", timeout=5) == ("claude", "결과")

    assert provider.last_attempts == {"claude": ["timed out after 5s", "ok"]}


def test_attempts_record_failures_when_every_provider_fails(
    monkeypatch: pytest.MonkeyPatch, no_cli: None
) -> None:
    def failing_urlopen(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(cli_provider.urllib.request, "urlopen", failing_urlopen)
    provider = CLIProvider(environment={})

    with pytest.raises(RuntimeError):
        provider.run("prompt", timeout=5)

    assert "ollama" in provider.last_attempts
    assert provider.last_attempts["ollama"] != "ok"


def test_context_warning_fires_above_the_threshold(
    monkeypatch: pytest.MonkeyPatch, no_cli: None
) -> None:
    install_urlopen(
        monkeypatch, {"message": {"content": "ok"}, "prompt_eval_count": 30000}
    )
    provider = CLIProvider(environment={})

    provider.run("prompt", timeout=5)

    assert provider.last_context_warning is not None
    assert "30000" in provider.last_context_warning
    assert "32768" in provider.last_context_warning


def test_context_warning_silent_below_the_threshold(
    monkeypatch: pytest.MonkeyPatch, no_cli: None
) -> None:
    install_urlopen(
        monkeypatch, {"message": {"content": "ok"}, "prompt_eval_count": 20000}
    )
    provider = CLIProvider(environment={})

    provider.run("prompt", timeout=5)

    assert provider.last_context_warning is None


def test_context_warning_silent_when_server_reports_no_token_count(
    monkeypatch: pytest.MonkeyPatch, no_cli: None
) -> None:
    install_urlopen(monkeypatch, {"message": {"content": "ok"}})
    provider = CLIProvider(environment={})

    provider.run("prompt", timeout=5)

    assert provider.last_context_warning is None


def test_openai_compatible_path_never_warns(
    monkeypatch: pytest.MonkeyPatch, no_cli: None
) -> None:
    install_urlopen(
        monkeypatch,
        {
            "choices": [{"message": {"content": "결과"}}],
            "usage": {"prompt_tokens": 999999},
        },
    )
    provider = CLIProvider(
        environment={
            "OLLAMA_SCREENING_MODEL": "off",
            "LOCAL_LLM_BASE_URL": "http://box:8080/v1",
            "LOCAL_LLM_MODEL": "llama",
        }
    )

    provider.run("prompt", timeout=5)

    assert provider.last_context_warning is None


@pytest.mark.parametrize("raw", ["0", "-1"])
def test_non_positive_num_ctx_is_rejected_at_resolve_time(raw: str) -> None:
    with pytest.raises(cli_provider.LocalLLMConfigError, match="must be positive"):
        resolve_local_llm({"OLLAMA_NUM_CTX": raw})


def test_non_positive_num_ctx_never_reaches_ollama(
    monkeypatch: pytest.MonkeyPatch, no_cli: None
) -> None:
    calls = forbid_urlopen(monkeypatch)
    provider = CLIProvider(environment={"OLLAMA_NUM_CTX": "0"})

    with pytest.raises(RuntimeError, match="must be positive"):
        provider.run("prompt", timeout=5)

    assert calls == []


def test_run_local_llm_returns_content_and_token_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_urlopen(
        monkeypatch, {"message": {"content": "본문"}, "prompt_eval_count": 1234}
    )

    content, tokens = run_local_llm(
        "ollama", "http://localhost:11434/api/chat", "m", {}, "p", 5
    )

    assert content == "본문"
    assert tokens == 1234
