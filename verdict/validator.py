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
    """Exact match, or shared stem/prefix (>=4 chars) so word forms line up:
    'slow'/'slower', 'mirror'/'mirrors', 'auth'/'authentication'."""
    if term in evidence:
        return True
    for ev in evidence:
        shorter, longer = (term, ev) if len(term) <= len(ev) else (ev, term)
        if len(shorter) >= 4 and longer.startswith(shorter):
            return True
    return False


def validate(scenarios: list[Scenario], diff: str, intent: str) -> list[ValidationResult]:
    evidence = _evidence_terms(diff, intent)
    results = []
    for scenario in scenarios:
        scenario_terms = _terms_of(f"{scenario.name} {scenario.description}")
        specific = scenario_terms - GENERIC_TERMS
        matched = sorted(t for t in specific if _matches(t, evidence))

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
