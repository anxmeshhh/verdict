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
# A provider under real rate-limit pressure (esp. free-tier requests/tokens
# per minute) can ask to wait far longer than our fixed backoff - respect
# that when it tells us, but don't let an interactive command hang forever.
RATE_LIMIT_MAX_WAIT = 30.0

API_KEY_ENV = "VERDICT_API_KEY"

# Some providers sit behind a WAF (Groq is behind Cloudflare) that blocks
# Python's default "Python-urllib/x.y" User-Agent as a bot - a valid key still
# gets a bare 403 with no auth-related detail at all. Identifying ourselves
# avoids that entirely.
USER_AGENT = "verdict-cli/0.1.0"

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


def _retry_delay(e: urllib.error.HTTPError, attempt: int) -> float:
    """Honor a standard Retry-After header (seconds) when the provider sends
    one - typical for real rate-limit responses - else fall back to our fixed
    backoff schedule."""
    header = e.headers.get("Retry-After") if e.headers else None
    if header:
        try:
            return min(float(header), RATE_LIMIT_MAX_WAIT)
        except ValueError:
            pass
    return BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)]


def _is_json_mode_rejection(status_code: int, detail: str) -> bool:
    """Some models (seen on Groq, e.g. certain Qwen builds) can't reliably
    produce output under a provider's enforced JSON mode and get a 400 back
    with an empty completion - not a bad key, not a bad request shape, just
    that model+response_format combination not working. Narrow match on
    purpose: this must never swallow an unrelated 400 (bad model id, bad
    diff, etc)."""
    return status_code == 400 and (
        "json_validate_failed" in detail or "failed_generation" in detail
    )


def _is_seed_unsupported(status_code: int, detail: str) -> bool:
    """Not every OpenAI-compatible layer accepts `seed` (confirmed live:
    Gemini's rejects it outright - 'Unknown name "seed": Cannot find field').
    Narrow match so this can't swallow an unrelated 400."""
    lowered = detail.lower()
    return status_code == 400 and "seed" in lowered and (
        "unknown" in lowered or "not supported" in lowered or "cannot find field" in lowered
    )


def _call_openai_compatible(
    prompt: str, config: Config, json_format: bool, temperature: float
) -> LLMResponse:
    base_url = resolve_base_url(config)
    api_key = resolve_api_key(config)
    use_json_mode = json_format
    use_seed = True  # pin sampling for reproducibility where the provider allows it

    def build_request() -> urllib.request.Request:
        payload: dict = {
            "model": config.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "stream": False,
        }
        if use_seed:
            payload["seed"] = 0
        if use_json_mode:
            payload["response_format"] = {"type": "json_object"}
        return urllib.request.Request(
            f"{base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "User-Agent": USER_AGENT,
            },
        )

    start = time.monotonic()
    last_error: Exception | None = None
    delay: float | None = None
    attempt = 1
    while attempt <= MAX_TRANSPORT_ATTEMPTS:
        try:
            req = build_request()
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
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")[:300]
            except OSError:
                pass
            if use_json_mode and _is_json_mode_rejection(e.code, detail):
                # Not transient, not a retry-with-backoff situation - the
                # model just can't do enforced JSON mode. Fall back to a
                # plain completion (the prompt already demands JSON-only
                # text) and try the exact same attempt again immediately.
                use_json_mode = False
                continue
            if use_seed and _is_seed_unsupported(e.code, detail):
                use_seed = False
                continue
            # 401/403 = bad key, 404 = bad model/base_url, 4xx generally our config - don't retry.
            # 429 (rate limit) and 5xx are transient - retry with backoff.
            if e.code < 500 and e.code != 429:
                raise LLMDown(
                    f"{config.provider} rejected the request (HTTP {e.code}): {detail or e.reason}"
                ) from e
            last_error = e
            delay = _retry_delay(e, attempt)
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            last_error = e
            delay = None

        if attempt < MAX_TRANSPORT_ATTEMPTS:
            time.sleep(delay if delay is not None else BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)])
        attempt += 1

    if isinstance(last_error, urllib.error.HTTPError) and last_error.code == 429:
        raise LLMDown(
            f"{config.provider} rate-limited this request (HTTP 429) after {MAX_TRANSPORT_ATTEMPTS} "
            "attempts - you're hitting the provider's requests/tokens-per-minute limit. Wait a bit "
            "and retry, or check your plan's rate limits."
        ) from last_error
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
            f"{base_url}/models",
            headers={"Authorization": f"Bearer {api_key}", "User-Agent": USER_AGENT},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        models = [str(m.get("id", "")) for m in body.get("data", []) if isinstance(m, dict)]
        known = None
        if models:
            known = any(config.model in m or m in config.model for m in models)
        return LLMStatus(True, models, model_known=known)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:200].strip()
        except OSError:
            pass
        return LLMStatus(False, [], f"HTTP {e.code}: {detail or e.reason}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        return LLMStatus(False, [], str(e))
