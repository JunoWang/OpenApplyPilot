import json

import httpx
import pytest

from applypilot.llm import (
    LLMClient,
    LLMConfigurationError,
    LLMSettings,
    resolve_settings,
)

MESSAGES = [
    {"role": "system", "content": "Follow the format."},
    {"role": "user", "content": "Score this job."},
]


def _client(settings: LLMSettings, handler) -> LLMClient:
    transport = httpx.MockTransport(handler)
    return LLMClient(settings, http_client=httpx.Client(transport=transport))


def test_openai_uses_max_completion_tokens_for_gpt5() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert request.url.path == "/v1/chat/completions"
        assert payload["max_completion_tokens"] == 512
        assert "max_tokens" not in payload
        assert "temperature" not in payload
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "SCORE: 8"}}]},
        )

    client = _client(
        LLMSettings("openai", "gpt-5.4-mini", "https://api.openai.test/v1", "secret"),
        handler,
    )

    assert client.chat(MESSAGES, max_tokens=512, temperature=0.2) == "SCORE: 8"


def test_anthropic_uses_native_messages_api() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert request.url.path == "/v1/messages"
        assert request.headers["x-api-key"] == "secret"
        assert payload["system"] == "Follow the format."
        assert payload["messages"] == [{"role": "user", "content": "Score this job."}]
        return httpx.Response(200, json={"content": [{"type": "text", "text": "SCORE: 7"}]})

    client = _client(
        LLMSettings("anthropic", "claude-test", "https://api.anthropic.test/v1", "secret"),
        handler,
    )

    assert client.chat(MESSAGES) == "SCORE: 7"


def test_gemini_uses_native_generate_content_api() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert request.url.path.endswith("/models/gemini-test:generateContent")
        assert request.url.params["key"] == "secret"
        assert payload["systemInstruction"]["parts"][0]["text"] == "Follow the format."
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"text": "SCORE: 9"}]}}]},
        )

    client = _client(
        LLMSettings("gemini", "gemini-test", "https://generativelanguage.test/v1beta", "secret"),
        handler,
    )

    assert client.chat(MESSAGES) == "SCORE: 9"


def test_ollama_uses_local_chat_api() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert request.url.path == "/api/chat"
        assert payload["stream"] is False
        assert payload["options"]["num_predict"] == 256
        return httpx.Response(200, json={"message": {"content": "SCORE: 6"}})

    client = _client(
        LLMSettings("ollama", "llama-test", "http://ollama.test"),
        handler,
    )

    assert client.chat(MESSAGES, max_tokens=256) == "SCORE: 6"


def test_stage_provider_and_model_override_global(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAPPLYPILOT_LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAPPLYPILOT_LLM_MODEL", "global-model")
    monkeypatch.setenv("OPENAPPLYPILOT_SCORE_PROVIDER", "anthropic")
    monkeypatch.setenv("OPENAPPLYPILOT_SCORE_MODEL", "score-model")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")

    settings = resolve_settings("score")

    assert settings.provider == "anthropic"
    assert settings.model == "score-model"


def test_multiple_implicit_providers_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "OPENAPPLYPILOT_LLM_PROVIDER",
        "LLM_PROVIDER",
        "GEMINI_API_KEY",
        "OLLAMA_BASE_URL",
        "LLM_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")

    with pytest.raises(LLMConfigurationError, match="Multiple LLM providers"):
        resolve_settings("score")
