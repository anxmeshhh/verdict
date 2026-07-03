"""
Provider-agnostic LLM transport - the single door every LLM call walks through.

Two routes, one contract:
- "ollama"  -> the local Ollama server (default; diffs never leave the machine)
- any OpenAI-compatible API -> openrouter, groq, gemini, openai, or a custom
  base_url (vLLM, LM Studio, ...) with the user's own API key

Both return the same LLMResponse (text + token counts + timing) so the
pipeline, token accounting, and audit trail never care which one ran.
"""
import json
import os
import time
import urllib.error
import urllib.request

from verdict import ollama
from verdict.config import Config
from verdict.ollama import LLMResponse

REQUEST_TIMEOUT = 300
MAX_TRANSPORT_ATTEMPTS = 3
BACKOFF_SECONDS = (1, 5)

API_KEY_ENV = "VERDICT_API_KEY"

# name -> OpenAI-compatible base URL (None = needs config.base_url)
PROVIDERS: dict[str, str | None] = {
    "ollama": None,
    "openrouter": "https://openrouter.ai/api/v1",
    "groq": "https://api.groq.com/openai/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "openai": "https://api.openai.com/v1",
    "custom": None,  # any OpenAI-compatible endpoint via config.base_url (vLLM, LM Studio, ...)
}


class LLMDown(Exception):
    """Transport failed - infrastructure, not a verdict."""


def is_local(config: Config) -> bool:
    return config.provider == "ollama"


def provider_label(config: Config) -> str:
    if is_local(config):
        return f"{config.model} [dim]@ ollama (local)[/]"
    return f"{config.model} [dim]@ {config.provider} (cloud)[/]"


def model_id(config: Config) -> str:
    """How the model is recorded in run records: unambiguous about where it ran."""
    if is_local(config):
        return config.model
    return f"{config.provider}/{config.model}"


def resolve_base_url(config: Config) -> str:
    if config.base_url:
        return config.base_url.rstrip("/")
    base = PROVIDERS.get(config.provider)
    if base is None:
        raise LLMDown(
            f"provider '{config.provider}' needs a base_url - set it with: verdict config set base_url <url>"
        )
    return base


def resolve_api_key(config: Config) -> str:
    key = os.environ.get(API_KEY_ENV, "").strip() or config.api_key.strip()
    if not key:
        raise LLMDown(
            f"provider '{config.provider}' needs an API key - "
            f"set {API_KEY_ENV} or run: verdict config set api_key <key>"
        )
    return key


def call(
    prompt: str,
    config: Config,
    json_format: bool = False,
    temperature: float = 0.0,
) -> LLMResponse:
    if is_local(config):
        try:
            return ollama.call(
                prompt, config.model, config.ollama_url,
                json_format=json_format, temperature=temperature,
            )
        except ollama.OllamaDown as e:
            raise LLMDown(str(e)) from e
    return _call_openai_compatible(prompt, config, json_format, temperature)


def _call_openai_compatible(
    prompt: str, config: Config, json_format: bool, temperature: float
) -> LLMResponse:
    base_url = resolve_base_url(config)
    api_key = resolve_api_key(config)
    payload: dict = {
        "model": config.model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "stream": False,
    }
    if json_format:
        payload["response_format"] = {"type": "json_object"}
    data = json.dumps(payload).encode("utf-8")

    start = time.monotonic()
    last_error: Exception | None = None
    for attempt in range(1, MAX_TRANSPORT_ATTEMPTS + 1):
        try:
            req = urllib.request.Request(
                f"{base_url}/chat/completions",
                data=data,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
            )
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            choices = body.get("choices") or []
            text = (choices[0].get("message") or {}).get("content", "") if choices else ""
            usage = body.get("usage") or {}
            return LLMResponse(
                text=text or "",
                prompt_tokens=int(usage.get("prompt_tokens", 0)),
                output_tokens=int(usage.get("completion_tokens", 0)),
                duration_s=round(time.monotonic() - start, 2),
                transport_attempts=attempt,
            )
        except urllib.error.HTTPError as e:
            # 401/403 = bad key, 404 = bad model/base_url, 4xx generally our config - don't retry.
            # 429 (rate limit) and 5xx are transient - retry with backoff.
            if e.code < 500 and e.code != 429:
                detail = ""
                try:
                    detail = e.read().decode("utf-8", errors="replace")[:300]
                except OSError:
                    pass
                raise LLMDown(
                    f"{config.provider} rejected the request (HTTP {e.code}): {detail or e.reason}"
                ) from e
            last_error = e
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            last_error = e

        if attempt < MAX_TRANSPORT_ATTEMPTS:
            time.sleep(BACKOFF_SECONDS[attempt - 1])

    raise LLMDown(
        f"{config.provider} unreachable after {MAX_TRANSPORT_ATTEMPTS} attempts: {last_error}"
    ) from last_error


class LLMStatus:
    def __init__(self, reachable: bool, models: list[str], error: str | None = None, model_known: bool | None = None):
        self.reachable = reachable
        self.models = models
        self.error = error
        # True/False when the provider lists models; None when we can't tell
        self.model_known = model_known


def check(config: Config, timeout: float = 8.0) -> LLMStatus:
    """Live health check for whichever provider is configured - never faked."""
    if is_local(config):
        from verdict.config import check_ollama

        s = check_ollama(config.ollama_url, timeout)
        return LLMStatus(s.reachable, s.models, s.error, model_known=(config.model in s.models) if s.reachable else None)
    try:
        base_url = resolve_base_url(config)
        api_key = resolve_api_key(config)
    except LLMDown as e:
        return LLMStatus(False, [], str(e))
    try:
        req = urllib.request.Request(
            f"{base_url}/models", headers={"Authorization": f"Bearer {api_key}"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        models = [str(m.get("id", "")) for m in body.get("data", []) if isinstance(m, dict)]
        known = None
        if models:
            known = any(config.model in m or m in config.model for m in models)
        return LLMStatus(True, models, model_known=known)
    except urllib.error.HTTPError as e:
        return LLMStatus(False, [], f"HTTP {e.code}: {e.reason}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        return LLMStatus(False, [], str(e))
