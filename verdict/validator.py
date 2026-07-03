"""
Module 4 - Scenario Validator.

Input:  scenarios (from 3a or 3b) + the diff they claim to verify
Output: per-scenario ValidationResult - traceable or not, with evidence

Deterministic logic only. A scenario is traceable when the terms it uses
(identifiers, file names, domain words) actually appear in the diff or in
the stated intent. This is the guard against LLM hallucination - Phase 0
showed the model inventing scenarios about functions not in the diff at
all when the intent was vague.
"""
import re
from dataclasses import dataclass

from verdict.generator import Scenario

# Terms too generic to count as evidence of traceability on their own
GENERIC_TERMS = {
    "test", "tests", "verify", "verifies", "check", "checks", "ensure",
    "ensures", "validate", "validates", "correct", "correctly", "handle",
    "handles", "handling", "error", "errors", "failure", "fails", "failed",
    "success", "successful", "successfully", "function", "method", "code",
    "change", "changes", "behavior", "case", "cases", "edge", "should",
    "must", "when", "after", "before", "with", "without", "that", "this",
    "the", "and", "for", "not", "are", "is", "does", "can", "will",
    "scenario", "expected", "works", "new", "old", "value", "values",
    "data", "file", "files", "run", "running", "runs", "call", "called",
    "calls", "return", "returns", "returned", "set", "sets", "get", "gets",
    "use", "uses", "used", "using", "make", "makes", "made", "still",
    "only", "also", "each", "all", "any", "one", "two", "first", "second",
}

MIN_MATCHED_TERMS = 2


@dataclass
class ValidationResult:
    scenario: Scenario
    traceable: bool
    matched_terms: list[str]
    reason: str


def _terms_of(text: str) -> set[str]:
    """Split text into lowercase word-level terms, including snake_case/camelCase pieces."""
    terms: set[str] = set()
    for raw in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", text):
        lowered = raw.lower()
        terms.add(lowered)
        # split snake_case and camelCase into pieces too
        for piece in re.split(r"_|(?<=[a-z])(?=[A-Z])", raw):
            if len(piece) >= 3:
                terms.add(piece.lower())
    return terms


def _evidence_terms(diff: str, intent: str) -> set[str]:
    """Terms appearing anywhere in the diff (changed AND context lines) or the intent.

    Context lines count: scenarios legitimately reference the surrounding code a
    change lands in. The validator is a hallucination guard, not the judge of
    scenario quality - the sandbox (Module 5) is the judge.
    """
    return _terms_of(diff) | _terms_of(intent)


def _matches(term: str, evidence: set[str]) -> bool:
    """Exact match, or a shared root (>=4 chars) anywhere in the other word so
    word forms line up: 'slow'/'slower', 'mirror'/'mirrors', 'auth'/
    'authentication', and - not just prefixes - 'digit'/'isdigit',
    'attr'/'hasattr', 'valid'/'invalid'. Real diffs are full of is-/has-/get-/
    in- prefixed identifiers whose plain-English root only ever appears
    embedded, never leading; a prefix-only check drops those every time."""
    if term in evidence:
        return True
    for ev in evidence:
        shorter, longer = (term, ev) if len(term) <= len(ev) else (ev, term)
        if len(shorter) >= 4 and shorter in longer:
            return True
    return False


_TYPE_ERROR_CLAIM_TERMS = ("typeerror", "type error", "valueerror", "value error")
_RUNTIME_TYPE_CHECK_PATTERN = re.compile(r"raise\s+(type|value)error|isinstance\s*\(", re.IGNORECASE)


def _claims_unenforced_type_check(scenario: Scenario, diff: str) -> bool:
    """Python type hints are NOT enforced at runtime - a scenario claiming a
    TypeError/ValueError gets raised purely from a type-hint mismatch is only
    plausible if the diff's ADDED lines contain an explicit runtime check
    (isinstance(...) / raise TypeError / raise ValueError somewhere). This is
    a real, observed hallucination class term-overlap traceability cannot
    catch on its own: the function name and even the exception name can
    legitimately appear in the diff/intent while the specific behavioral
    claim is still invented outright. Phase 0's original traceability guard
    was built to catch scenarios about code that ISN'T in the diff at all -
    not false claims about code that IS in the diff, which is a different
    failure mode needing its own targeted check (same pattern as the
    dead-function and broken-monkeypatch checks in testgen.py)."""
    text = f"{scenario.name} {scenario.description}".lower()
    if not any(t in text for t in _TYPE_ERROR_CLAIM_TERMS):
        return False
    added_lines = "\n".join(
        line[1:] for line in diff.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    return not _RUNTIME_TYPE_CHECK_PATTERN.search(added_lines)


def validate(scenarios: list[Scenario], diff: str, intent: str) -> list[ValidationResult]:
    evidence = _evidence_terms(diff, intent)
    results = []
    for scenario in scenarios:
        scenario_terms = _terms_of(f"{scenario.name} {scenario.description}")
        specific = scenario_terms - GENERIC_TERMS
        matched = sorted(t for t in specific if _matches(t, evidence))

        if _claims_unenforced_type_check(scenario, diff):
            results.append(
                ValidationResult(
                    scenario=scenario,
                    traceable=False,
                    matched_terms=matched,
                    reason=(
                        "scenario claims a TypeError/ValueError from a type-hint mismatch, but "
                        "Python does not enforce type hints at runtime and the diff has no "
                        "explicit isinstance()/raise check for it - not supported by the code, "
                        "regardless of shared terms"
                    ),
                )
            )
            continue

        if len(matched) >= MIN_MATCHED_TERMS:
            results.append(
                ValidationResult(
                    scenario=scenario,
                    traceable=True,
                    matched_terms=matched,
                    reason=f"traced via: {', '.join(matched[:6])}",
                )
            )
        else:
            results.append(
                ValidationResult(
                    scenario=scenario,
                    traceable=False,
                    matched_terms=matched,
                    reason=(
                        "scenario references almost nothing that appears in the diff "
                        f"or intent (matched only: {matched or 'nothing'})"
                    ),
                )
            )
    return results
