"""
Test-code generation - turns a validated scenario into an executable check.

Section 11 of the direction doc: "Sandbox containers execute LLM-generated
test code against real repositories." This is that generation step - the
scenario (English) becomes a self-contained Python script that exits 0 when
the scenario holds and non-zero when it does not. The script is part of the
audit trail and is stored as evidence alongside the run.
"""
import ast
import re
from dataclasses import dataclass

from verdict import llm
from verdict.config import Config
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
- Write the check as code that runs immediately - if you put it inside a
  function, you MUST call that function right after defining it. A function
  that is defined but never called means your check never executes, no
  matter what it contains.
- Exit code 0 means the scenario HOLDS. Any exception or sys.exit(1) means it FAILS.
- Print one line explaining what was checked and what was found.
- Import the repo's own modules directly when needed (the repo root is on sys.path).
  Prefer a direct `from module import thing` naming the exact function/class shown
  in the diff - the diff headers already tell you which file changed, so a direct
  import is almost always possible and removes any ambiguity about which callable
  you mean. Only fall back to scanning the repository for a matching callable by
  name if a direct import is genuinely not possible.
- If you do locate a callable dynamically (by name-matching instead of a direct
  import), NEVER assume its argument order from the parameter count alone. Call
  `inspect.signature(obj).parameters` to get the actual parameter NAMES, match
  each one to what the scenario is about (e.g. a parameter named `ip` vs `password`
  vs `username`), and pass arguments as keywords matched by name
  (`func(username=..., ip=..., password=...)`), not positionally by guessed order.
  A positional call to a guessed function can silently test the wrong thing (e.g.
  swap `ip` and `password`) while still producing a confident-looking exit code.
- The sandbox has NO network and NO live services: no Ollama, no databases,
  no HTTP servers. Never call anything that needs one. Verify behavior by
  importing modules and calling functions directly, monkeypatching any
  function that would touch a service.
- If you import a name directly (`from module import thing`), do NOT also
  try to patch it as a module attribute (`module.thing = fake`) - that
  assignment never affects the name you already imported directly, so the
  fake is silently never used. Either patch consistently with
  `unittest.mock.patch("module.thing", fake)`, or only ever refer to it as
  `module.thing` (never import it directly) if you intend to patch it.
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
    prompt_tokens: int = 0
    output_tokens: int = 0
    llm_duration_s: float = 0.0


def _strip_fences(text: str) -> str:
    # "Thinking"/reasoning models can prepend a <think>...</think> trace to
    # the content before the actual code - drop it first, same fix as
    # generator.py's _extract_json.
    text = re.sub(r"^<think>.*?</think>\s*", "", text.strip(), flags=re.DOTALL)
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


def find_dead_functions(code: str) -> list[str]:
    """Deterministic structural check: a top-level function the script defines
    but never references again is dead code - any assertions inside it never
    run, so a clean exit code proves nothing about the scenario at all. This
    is exactly how a PASSED verdict can be wrong without any exception ever
    being raised."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []  # lint_test_code's syntax check already blocks this case

    top_level = {
        node.name
        for node in ast.iter_child_nodes(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    if not top_level:
        return []

    # ast.Name only fires at USE sites (calls, references, decorators) - the
    # `def foo():` binding itself is a plain string attribute, never a Name
    # node - so anything found here is a genuine reference, not the def line.
    referenced = {
        node.id for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id in top_level
    }
    return sorted(top_level - referenced)


def find_broken_monkeypatch(code: str) -> list[str]:
    """Deterministic check for the classic Python footgun: `from X import Y`
    binds Y directly in this file's namespace, so a later `X.Y = fake`
    assignment patches the module object but never touches that already-bound
    name - every subsequent bare `Y(...)` call still runs the real Y, while
    the test believes it's exercising the fake. This reproduces consistently
    (same wrong assumption every time), so the FAILED-confirmation pass
    cannot catch it - regeneration just makes the same mistake again. This
    check catches the pattern the confirmation pass structurally can't."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []  # lint_test_code's syntax check already blocks this case

    direct_imports: dict[str, str] = {}  # name -> module it was imported from
    module_aliases: dict[str, str] = {}  # local alias -> module name

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                direct_imports[alias.asname or alias.name] = node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                module_aliases[alias.asname or alias.name] = alias.name

    bare_calls = {
        node.func.id for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    problems = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)):
                continue
            module_alias = target.value.id
            attr_name = target.attr
            if module_aliases.get(module_alias) != direct_imports.get(attr_name):
                continue
            if attr_name in bare_calls:
                problems.append(
                    f"'{module_alias}.{attr_name} = ...' patches the module attribute, but "
                    f"'{attr_name}' was imported directly via 'from {direct_imports[attr_name]} "
                    f"import {attr_name}' and is later called as a bare '{attr_name}(...)' - that "
                    f"call uses the real, unpatched function; the patch has no effect on it."
                )
    return problems


MAX_ATTEMPTS = 3


def generate_test_code(
    scenario: Scenario,
    intent_result: IntentResult,
    config: Config,
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
    prompt_tokens = output_tokens = 0
    llm_duration = 0.0
    code = ""
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
        try:
            resp = llm.call(retry_prompt, config)
        except llm.LLMDown as e:
            raise GenerationError(str(e), prompt=prompt, provider_error=True) from e
        code = _strip_fences(resp.text)
        prompt_tokens += resp.prompt_tokens
        output_tokens += resp.output_tokens
        llm_duration += resp.duration_s
        if not code.strip():
            problems = ["returned empty code"]
            continue
        problems = lint_test_code(code)
        dead = find_dead_functions(code)
        if dead:
            problems = problems + [
                f"function(s) defined but never called: {', '.join(dead)} - "
                "any assertions inside them never run, so the exit code would "
                "prove nothing. Call them immediately after defining them, or "
                "write the check as top-level code with no function wrapper."
            ]
        problems = problems + find_broken_monkeypatch(code)
        if not problems:
            return GeneratedTest(
                scenario=scenario,
                code=code,
                prompt=prompt,
                attempts=attempt,
                prompt_tokens=prompt_tokens,
                output_tokens=output_tokens,
                llm_duration_s=round(llm_duration, 2),
            )

    raise GenerationError(
        f"generated test code still broken after {MAX_ATTEMPTS} attempts: {'; '.join(problems[:3])}",
        prompt=prompt,
        raw_response=code,
    )
