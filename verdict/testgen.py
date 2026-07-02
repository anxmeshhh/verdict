"""
Test-code generation - turns a validated scenario into an executable check.

Section 11 of the direction doc: "Sandbox containers execute LLM-generated
test code against real repositories." This is that generation step - the
scenario (English) becomes a self-contained Python script that exits 0 when
the scenario holds and non-zero when it does not. The script is part of the
audit trail and is stored as evidence alongside the run.
"""
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

from verdict.generator import GenerationError, Scenario
from verdict.intent import IntentResult

MAX_DIFF_CHARS = 20_000
GENERATION_TIMEOUT = 300

PROMPT_TEMPLATE = """You are writing an automated check for a code change.

Stated intent of the change:
{intent}

Diff:
{diff}

Scenario to verify:
  name: {name}
  description: {description}

Write ONE self-contained Python test script that verifies this scenario
against the repository, which is available at /app (current working
directory). Rules:
- Plain Python, no pytest. Use assert statements or explicit checks.
- Exit code 0 means the scenario HOLDS. Any exception or sys.exit(1) means it FAILS.
- Print one line explaining what was checked and what was found.
- Import the repo's own modules directly when needed (the repo root is on sys.path).
- The sandbox has NO network and NO live services: no Ollama, no databases,
  no HTTP servers. Never call anything that needs one. Verify behavior by
  importing modules and calling functions directly, monkeypatching any
  function that would touch a service.
- Import every module you use. Do not write outside /tmp.
- If the scenario cannot be checked by code at all, print why and call sys.exit(2).

Respond with ONLY the Python code, no markdown fences, no explanation.
"""


@dataclass
class GeneratedTest:
    scenario: Scenario
    code: str
    prompt: str
    attempts: int = 1


def _strip_fences(text: str) -> str:
    text = text.strip()
    match = re.match(r"^```(?:python)?\s*\n(.*?)\n?```\s*$", text, re.DOTALL)
    return match.group(1) if match else text


def lint_test_code(code: str) -> list[str]:
    """Deterministic gate: catch broken generated code (syntax errors, undefined
    names like a missing import) BEFORE it runs. A broken check must never be
    reported as a failed change."""
    import io

    from pyflakes.api import check as pyflakes_check
    from pyflakes.reporter import Reporter

    out, err = io.StringIO(), io.StringIO()
    pyflakes_check(code, "scenario_test.py", Reporter(out, err))
    problems = [line for line in (out.getvalue() + err.getvalue()).splitlines() if line.strip()]
    # only hard problems block execution; style noise does not
    blocking = [p for p in problems if "undefined name" in p or "syntax" in p.lower()]
    return blocking


MAX_ATTEMPTS = 3


def _call_model(prompt: str, model: str, ollama_url: str) -> str:
    payload = json.dumps(
        {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.2},
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{ollama_url}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=GENERATION_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError) as e:
        raise GenerationError(f"Ollama unreachable during test generation: {e}", prompt=prompt) from e
    return _strip_fences(body["response"])


def generate_test_code(
    scenario: Scenario,
    intent_result: IntentResult,
    model: str,
    ollama_url: str,
) -> GeneratedTest:
    diff = intent_result.diff
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + "\n... (diff truncated)"

    prompt = PROMPT_TEMPLATE.format(
        intent=intent_result.intent,
        diff=diff,
        name=scenario.name,
        description=scenario.description,
    )

    problems: list[str] = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        retry_prompt = prompt
        if problems:
            retry_prompt = (
                prompt
                + "\n\nYour previous attempt was rejected for these problems:\n"
                + "\n".join(problems)
                + "\n\nFix ALL of them. For every 'undefined name X': either add the"
                " correct import for X (if X comes from the repo, import it from the"
                " module path shown in the diff headers), or define X yourself, or"
                " stop using it. Every single name you reference must be imported"
                " or defined in your script."
            )
        code = _call_model(retry_prompt, model, ollama_url)
        if not code.strip():
            problems = ["returned empty code"]
            continue
        problems = lint_test_code(code)
        if not problems:
            return GeneratedTest(scenario=scenario, code=code, prompt=prompt, attempts=attempt)

    raise GenerationError(
        f"generated test code still broken after {MAX_ATTEMPTS} attempts: {'; '.join(problems[:3])}",
        prompt=prompt,
        raw_response=code,
    )
