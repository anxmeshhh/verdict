"""
Module 3a - Scenario Generator (autonomous).

Input:  IntentResult {diff, intent} (must not be vague)
Output: GenerationResult - scenarios plus the full audit trail
        (exact prompt, model, raw response) per Section 13:
        every run must be traceable without guessing.

This is the ONE bounded LLM step in the entire pipeline.
"""
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from verdict.intent import IntentResult

MAX_DIFF_CHARS = 24_000  # keep well inside the 7B model's context window
GENERATION_TIMEOUT = 180
MAX_ATTEMPTS = 2

PROMPT_TEMPLATE = """You are reviewing a code change to verify it does what it claims.

Stated intent (from the commit message or PR description):
{intent}

Diff:
{diff}

Propose 2-5 concrete test scenarios that would verify whether this change
actually fulfills the stated intent. Each scenario must be something you
could plausibly check by running code against this diff - not a vague
suggestion. Respond with ONLY valid JSON in this exact shape, no other text:

{{"scenarios": [{{"name": "short_snake_case_name", "description": "one sentence describing what is being verified"}}]}}
"""


@dataclass
class Scenario:
    name: str
    description: str


@dataclass
class GenerationResult:
    scenarios: list[Scenario]
    model: str
    prompt: str
    raw_response: str
    attempts: int = 1
    source: str = "llm"  # vs "manual" from Module 3b


@dataclass
class GenerationError(Exception):
    message: str
    prompt: str = ""
    raw_response: str = ""

    def __str__(self) -> str:
        return self.message


def build_prompt(intent_result: IntentResult) -> str:
    diff = intent_result.diff
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + "\n... (diff truncated)"
    return PROMPT_TEMPLATE.format(intent=intent_result.intent, diff=diff)


def _call_ollama(ollama_url: str, model: str, prompt: str) -> str:
    payload = json.dumps(
        {
            "model": model,
            "prompt": prompt,
            "format": "json",
            "stream": False,
            "options": {"temperature": 0.2},
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{ollama_url}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=GENERATION_TIMEOUT) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return body["response"]


def _parse_scenarios(raw: str) -> list[Scenario]:
    parsed = json.loads(raw)
    items = parsed.get("scenarios")
    if not isinstance(items, list) or not items:
        raise ValueError("response JSON has no non-empty 'scenarios' list")
    scenarios = []
    for item in items:
        name = str(item.get("name", "")).strip()
        description = str(item.get("description", "")).strip()
        if not name or not description:
            raise ValueError(f"scenario missing name or description: {item!r}")
        scenarios.append(Scenario(name=name, description=description))
    return scenarios


def generate(intent_result: IntentResult, model: str, ollama_url: str) -> GenerationResult:
    """Generate scenarios for a clear intent. Refuses vague intent - silence beats a wrong verdict."""
    if intent_result.vague:
        raise GenerationError(
            f"intent is too vague to verify against: {intent_result.vague_reason}. "
            "State the intent explicitly (--intent) or author scenarios manually."
        )
    if not intent_result.diff.strip():
        raise GenerationError("diff is empty - nothing to verify")

    prompt = build_prompt(intent_result)
    last_error = ""
    raw = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            raw = _call_ollama(ollama_url, model, prompt)
            scenarios = _parse_scenarios(raw)
            return GenerationResult(
                scenarios=scenarios,
                model=model,
                prompt=prompt,
                raw_response=raw,
                attempts=attempt,
            )
        except (urllib.error.URLError, OSError) as e:
            raise GenerationError(f"Ollama unreachable during generation: {e}", prompt=prompt) from e
        except (ValueError, KeyError, json.JSONDecodeError) as e:
            last_error = str(e)

    raise GenerationError(
        f"model returned unusable JSON after {MAX_ATTEMPTS} attempts: {last_error}",
        prompt=prompt,
        raw_response=raw,
    )
