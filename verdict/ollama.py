"""
Single Ollama transport for every LLM call in the pipeline.

One place for: retry with backoff on transient failures (connection refused,
5xx, cold-load hiccups), token accounting (Ollama reports prompt/output token
counts on every response - they are part of the audit trail, not garbage),
and timing.
"""
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

REQUEST_TIMEOUT = 300
MAX_TRANSPORT_ATTEMPTS = 3
BACKOFF_SECONDS = (1, 5)  # wait before attempt 2, attempt 3


@dataclass
class LLMResponse:
    text: str
    prompt_tokens: int
    output_tokens: int
    duration_s: float
    transport_attempts: int


class OllamaDown(Exception):
    """Transport failed even after retries - infrastructure, not a verdict."""


def call(
    prompt: str,
    model: str,
    ollama_url: str,
    json_format: bool = False,
    temperature: float = 0.2,
) -> LLMResponse:
    payload: dict = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": temperature},
    }
    if json_format:
        payload["format"] = "json"
    data = json.dumps(payload).encode("utf-8")

    start = time.monotonic()
    last_error: Exception | None = None
    for attempt in range(1, MAX_TRANSPORT_ATTEMPTS + 1):
        try:
            req = urllib.request.Request(
                f"{ollama_url}/api/generate",
                data=data,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            return LLMResponse(
                text=body.get("response", ""),
                prompt_tokens=int(body.get("prompt_eval_count", 0)),
                output_tokens=int(body.get("eval_count", 0)),
                duration_s=round(time.monotonic() - start, 2),
                transport_attempts=attempt,
            )
        except urllib.error.HTTPError as e:
            # 5xx (e.g. model cold-load failure) is retryable; 4xx is our bug - don't retry
            if e.code < 500:
                raise OllamaDown(f"Ollama rejected the request (HTTP {e.code}): {e.reason}") from e
            last_error = e
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            last_error = e

        if attempt < MAX_TRANSPORT_ATTEMPTS:
            time.sleep(BACKOFF_SECONDS[attempt - 1])

    raise OllamaDown(
        f"Ollama unreachable after {MAX_TRANSPORT_ATTEMPTS} attempts: {last_error}"
    ) from last_error
