"""Provider-aware LLM client used by OpenApplyPilot pipeline stages.

The pipeline keeps provider selection explicit and never falls back to a
different provider after a request fails. Each stage may select its own
provider and model with environment variables such as::

    OPENAPPLYPILOT_SCORE_PROVIDER=openai
    OPENAPPLYPILOT_SCORE_MODEL=gpt-5.4-mini

Global ``OPENAPPLYPILOT_LLM_PROVIDER`` and ``OPENAPPLYPILOT_LLM_MODEL``
values are used when a stage override is absent. The legacy ``LLM_MODEL``
and ``LLM_URL`` variables remain supported for existing installations.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)

SUPPORTED_PROVIDERS = ("openai", "anthropic", "gemini", "ollama", "openai_compatible")
PIPELINE_STAGES = ("discover", "enrich", "score", "tailor", "cover")

_MAX_RETRIES = 5
_TIMEOUT = 120
_RATE_LIMIT_BASE_WAIT = 10

_DEFAULT_MODELS = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-sonnet-4-5",
    "gemini": "gemini-2.0-flash",
    "ollama": "llama3.2",
    "openai_compatible": "local-model",
}

_DEFAULT_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "ollama": "http://127.0.0.1:11434",
}


@dataclass(frozen=True)
class LLMSettings:
    """Resolved settings for one provider/model pair."""

    provider: str
    model: str
    base_url: str
    api_key: str = ""


class LLMConfigurationError(RuntimeError):
    """Raised when provider selection is missing or ambiguous."""


class LLMRequestError(RuntimeError):
    """Provider request failure with a safe, actionable error message."""

    def __init__(
        self,
        provider: str,
        model: str,
        message: str,
        *,
        status_code: int | None = None,
    ) -> None:
        self.provider = provider
        self.model = model
        self.status_code = status_code
        status = f" HTTP {status_code}" if status_code is not None else ""
        super().__init__(f"{provider}/{model}{status}: {message}")


def _normalise_provider(value: str) -> str:
    aliases = {
        "claude": "anthropic",
        "google": "gemini",
        "local": "ollama",
        "openai-compatible": "openai_compatible",
    }
    provider = aliases.get(value.strip().lower(), value.strip().lower())
    if provider not in SUPPORTED_PROVIDERS:
        raise LLMConfigurationError(
            f"Unsupported LLM provider '{value}'. Choose one of: "
            f"{', '.join(SUPPORTED_PROVIDERS)}."
        )
    return provider


def _configured_providers() -> list[str]:
    configured: list[str] = []
    if os.environ.get("OPENAI_API_KEY"):
        configured.append("openai")
    if os.environ.get("ANTHROPIC_API_KEY"):
        configured.append("anthropic")
    if os.environ.get("GEMINI_API_KEY"):
        configured.append("gemini")
    if os.environ.get("OLLAMA_BASE_URL"):
        configured.append("ollama")
    if os.environ.get("LLM_URL"):
        configured.append("openai_compatible")
    return configured


def _resolve_provider(stage: str | None = None) -> str:
    stage_name = (stage or "").strip().lower()
    if stage_name and stage_name not in PIPELINE_STAGES:
        raise LLMConfigurationError(
            f"Unknown LLM pipeline stage '{stage}'. Choose one of: {', '.join(PIPELINE_STAGES)}."
        )

    stage_provider = (
        os.environ.get(f"OPENAPPLYPILOT_{stage_name.upper()}_PROVIDER", "")
        if stage_name
        else ""
    )
    explicit = (
        stage_provider
        or os.environ.get("OPENAPPLYPILOT_LLM_PROVIDER", "")
        or os.environ.get("LLM_PROVIDER", "")
    )
    if explicit:
        return _normalise_provider(explicit)

    configured = _configured_providers()
    if len(configured) == 1:
        return configured[0]
    if len(configured) > 1:
        raise LLMConfigurationError(
            "Multiple LLM providers are configured. Set OPENAPPLYPILOT_LLM_PROVIDER "
            "or an OPENAPPLYPILOT_<STAGE>_PROVIDER value to choose explicitly."
        )
    raise LLMConfigurationError(
        "No LLM provider configured. Set OPENAPPLYPILOT_LLM_PROVIDER and the "
        "matching API key, or configure OLLAMA_BASE_URL for a local model."
    )


def resolve_settings(stage: str | None = None) -> LLMSettings:
    """Resolve a stage-specific provider configuration from the environment."""
    provider = _resolve_provider(stage)
    stage_name = (stage or "").strip().upper()
    stage_model = os.environ.get(f"OPENAPPLYPILOT_{stage_name}_MODEL", "") if stage_name else ""
    model = (
        stage_model
        or os.environ.get("OPENAPPLYPILOT_LLM_MODEL", "")
        or os.environ.get("LLM_MODEL", "")
        or _DEFAULT_MODELS[provider]
    )

    if provider == "openai":
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            raise LLMConfigurationError("OPENAI_API_KEY is required for the openai provider.")
        return LLMSettings(
            provider=provider,
            model=model,
            base_url=os.environ.get("OPENAI_BASE_URL", _DEFAULT_BASE_URLS[provider]).rstrip("/"),
            api_key=api_key,
        )
    if provider == "anthropic":
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise LLMConfigurationError(
                "ANTHROPIC_API_KEY is required for the anthropic provider."
            )
        return LLMSettings(
            provider=provider,
            model=model,
            base_url=os.environ.get("ANTHROPIC_BASE_URL", _DEFAULT_BASE_URLS[provider]).rstrip("/"),
            api_key=api_key,
        )
    if provider == "gemini":
        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            raise LLMConfigurationError("GEMINI_API_KEY is required for the gemini provider.")
        return LLMSettings(
            provider=provider,
            model=model,
            base_url=os.environ.get("GEMINI_BASE_URL", _DEFAULT_BASE_URLS[provider]).rstrip("/"),
            api_key=api_key,
        )
    if provider == "ollama":
        return LLMSettings(
            provider=provider,
            model=model,
            base_url=os.environ.get("OLLAMA_BASE_URL", _DEFAULT_BASE_URLS[provider]).rstrip("/"),
        )

    base_url = os.environ.get("LLM_URL", "").rstrip("/")
    if not base_url:
        raise LLMConfigurationError("LLM_URL is required for the openai_compatible provider.")
    return LLMSettings(
        provider=provider,
        model=model,
        base_url=base_url,
        api_key=os.environ.get("LLM_API_KEY", ""),
    )


class LLMClient:
    """Small provider-native client with one stable pipeline-facing API."""

    def __init__(
        self,
        settings: LLMSettings,
        *,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.settings = settings
        self.provider = settings.provider
        self.base_url = settings.base_url
        self.model = settings.model
        self.api_key = settings.api_key
        self._client = http_client or httpx.Client(timeout=_TIMEOUT)
        self._owns_client = http_client is None

    def _post(self, url: str, *, payload: dict[str, Any], headers: dict[str, str]) -> dict:
        response = self._client.post(url, json=payload, headers=headers)
        if response.is_error:
            detail = response.text.strip().replace("\n", " ")[:500] or response.reason_phrase
            raise LLMRequestError(
                self.provider,
                self.model,
                detail,
                status_code=response.status_code,
            )
        try:
            return response.json()
        except ValueError as exc:
            raise LLMRequestError(
                self.provider,
                self.model,
                "Provider returned a non-JSON response.",
                status_code=response.status_code,
            ) from exc

    @staticmethod
    def _openai_content(data: dict[str, Any]) -> str:
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("Missing choices[0].message.content in provider response.") from exc
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                str(part.get("text", "")) for part in content if isinstance(part, dict)
            )
        return str(content)

    def _chat_openai(self, messages: list[dict], temperature: float, max_tokens: int) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            # Current OpenAI reasoning and GPT-5 family models reject max_tokens.
            "max_completion_tokens": max_tokens,
        }
        if not self.model.lower().startswith(("gpt-5", "o1", "o3", "o4")):
            payload["temperature"] = temperature
        data = self._post(
            f"{self.base_url}/chat/completions",
            payload=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            return self._openai_content(data)
        except ValueError as exc:
            raise LLMRequestError(self.provider, self.model, str(exc)) from exc

    def _chat_openai_compatible(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
    ) -> str:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        data = self._post(
            f"{self.base_url}/chat/completions",
            payload={
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
            headers=headers,
        )
        try:
            return self._openai_content(data)
        except ValueError as exc:
            raise LLMRequestError(self.provider, self.model, str(exc)) from exc

    def _chat_anthropic(self, messages: list[dict], temperature: float, max_tokens: int) -> str:
        system_parts = [str(msg.get("content", "")) for msg in messages if msg.get("role") == "system"]
        conversation = [
            {"role": msg["role"], "content": msg.get("content", "")}
            for msg in messages
            if msg.get("role") in {"user", "assistant"}
        ]
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": conversation,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        data = self._post(
            f"{self.base_url}/messages",
            payload=payload,
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
        )
        parts = data.get("content", [])
        text = "".join(
            str(part.get("text", ""))
            for part in parts
            if isinstance(part, dict) and part.get("type") == "text"
        )
        if not text:
            raise LLMRequestError(self.provider, self.model, "Missing text content in provider response.")
        return text

    def _chat_gemini(self, messages: list[dict], temperature: float, max_tokens: int) -> str:
        contents: list[dict[str, Any]] = []
        system_parts: list[dict[str, str]] = []
        for message in messages:
            role = message.get("role")
            content = str(message.get("content", ""))
            if role == "system":
                system_parts.append({"text": content})
            elif role in {"user", "assistant"}:
                contents.append({
                    "role": "model" if role == "assistant" else "user",
                    "parts": [{"text": content}],
                })

        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        if system_parts:
            payload["systemInstruction"] = {"parts": system_parts}
        data = self._post(
            f"{self.base_url}/models/{self.model}:generateContent?key={self.api_key}",
            payload=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            return "".join(
                str(part.get("text", ""))
                for part in data["candidates"][0]["content"]["parts"]
                if isinstance(part, dict)
            )
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMRequestError(
                self.provider,
                self.model,
                "Missing candidate text in provider response.",
            ) from exc

    def _chat_ollama(self, messages: list[dict], temperature: float, max_tokens: int) -> str:
        data = self._post(
            f"{self.base_url}/api/chat",
            payload={
                "model": self.model,
                "messages": messages,
                "stream": False,
                "options": {"temperature": temperature, "num_predict": max_tokens},
            },
            headers={"Content-Type": "application/json"},
        )
        try:
            return str(data["message"]["content"])
        except (KeyError, TypeError) as exc:
            raise LLMRequestError(
                self.provider,
                self.model,
                "Missing message.content in Ollama response.",
            ) from exc

    def _chat_once(self, messages: list[dict], temperature: float, max_tokens: int) -> str:
        if self.provider == "openai":
            return self._chat_openai(messages, temperature, max_tokens)
        if self.provider == "anthropic":
            return self._chat_anthropic(messages, temperature, max_tokens)
        if self.provider == "gemini":
            return self._chat_gemini(messages, temperature, max_tokens)
        if self.provider == "ollama":
            return self._chat_ollama(messages, temperature, max_tokens)
        return self._chat_openai_compatible(messages, temperature, max_tokens)

    def chat(
        self,
        messages: list[dict],
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> str:
        """Send a request to the configured provider without cross-provider fallback."""
        request_messages = messages
        if "qwen" in self.model.lower() and messages:
            first = messages[0]
            if first.get("role") == "user" and not str(first.get("content", "")).startswith("/no_think"):
                request_messages = [
                    {"role": "user", "content": f"/no_think\n{first.get('content', '')}"},
                    *messages[1:],
                ]

        for attempt in range(_MAX_RETRIES):
            try:
                return self._chat_once(request_messages, temperature, max_tokens)
            except LLMRequestError as exc:
                if exc.status_code not in {429, 503} or attempt == _MAX_RETRIES - 1:
                    raise
                wait = min(_RATE_LIMIT_BASE_WAIT * (2**attempt), 60)
                log.warning(
                    "%s rate limited; retrying in %ds (%d/%d)",
                    self.provider,
                    wait,
                    attempt + 1,
                    _MAX_RETRIES,
                )
                time.sleep(wait)
            except httpx.TimeoutException as exc:
                if attempt == _MAX_RETRIES - 1:
                    raise LLMRequestError(
                        self.provider,
                        self.model,
                        "Request timed out after all retries.",
                    ) from exc
                wait = min(_RATE_LIMIT_BASE_WAIT * (2**attempt), 60)
                log.warning(
                    "%s request timed out; retrying in %ds (%d/%d)",
                    self.provider,
                    wait,
                    attempt + 1,
                    _MAX_RETRIES,
                )
                time.sleep(wait)
            except httpx.RequestError as exc:
                raise LLMRequestError(
                    self.provider,
                    self.model,
                    f"Network request failed: {exc.__class__.__name__}.",
                ) from exc

        raise LLMRequestError(self.provider, self.model, "Request failed after all retries.")

    def ask(self, prompt: str, **kwargs: Any) -> str:
        return self.chat([{"role": "user", "content": prompt}], **kwargs)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


_instances: dict[LLMSettings, LLMClient] = {}


def get_client(stage: str | None = None) -> LLMClient:
    """Return a cached client for the selected pipeline stage."""
    settings = resolve_settings(stage)
    client = _instances.get(settings)
    if client is None:
        log.info(
            "LLM stage=%s provider=%s model=%s",
            stage or "default",
            settings.provider,
            settings.model,
        )
        client = LLMClient(settings)
        _instances[settings] = client
    return client


def reset_clients() -> None:
    """Close cached clients. Primarily useful for config reloads and tests."""
    for client in _instances.values():
        client.close()
    _instances.clear()
