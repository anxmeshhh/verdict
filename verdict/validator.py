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


# Recurring shape of hallucination: the model treats an IMPLICIT signal (a
# type hint, a docstring word, a function's name) as if it were an EXPLICIT
# behavioral guarantee, and invents a scenario asserting that guarantee.
# Term-overlap traceability cannot catch this on its own - the function name,
# and often even the claimed behavior's own vocabulary, legitimately appear
# in the diff while the specific claim is still invented outright (Phase 0's
# original guard was built for scenarios about code that ISN'T in the diff at
# all - a different failure mode from a false claim about code that IS).
#
# Each entry: if a scenario's text matches `claim`, the diff's ADDED lines
# must contain something matching `evidence`, or the scenario is rejected
# regardless of term overlap. This is deliberately a small, named, growable
# list in the same spirit as find_dead_functions/find_broken_monkeypatch -
# not a general "does this claim logically follow from the diff" solver
# (that needs semantic understanding, which is out of scope for a
# deterministic validator). It covers the specific patterns observed live
# plus the ones most likely to recur next; a genuinely novel invented claim
# outside these categories will still slip through, same as any pattern list.
_UNSUPPORTED_BEHAVIOR_CLAIMS = [
    (
        "type-enforcement",
        re.compile(r"\btypeerror\b|\bvalueerror\b|\btype[- ]?error\b|\bvalue[- ]?error\b", re.IGNORECASE),
        re.compile(r"raise\s+(type|value)error|isinstance\s*\(", re.IGNORECASE),
        "claims a TypeError/ValueError from a type-hint mismatch, but Python does not "
        "enforce type hints at runtime and the diff has no explicit isinstance()/raise "
        "check for it",
    ),
    (
        "logging-on-failure",
        # "log(s|ging|ged)" NOT immediately followed by in/out (excludes the
        # "user logging in"/"logged out" auth sense) AND near a failure/event
        # word - caught live: "verify a user logging in" false-positived on a
        # bare \blogs?\b before this was narrowed
        re.compile(
            r"\blog(?:s|ged|ging)?\b(?!\s*(?:in|out)\b).{0,25}\b(fail|error|exception|warn|event|audit)\w*\b"
            r"|\b(fail|error|exception|warn|event|audit)\w*\b.{0,25}\blog(?:s|ged|ging)?\b(?!\s*(?:in|out)\b)",
            re.IGNORECASE,
        ),
        re.compile(r"\blogging\.|\blogger\.|\blog\.(debug|info|warning|error|exception)\s*\(", re.IGNORECASE),
        "claims a failure/event is logged, but the diff has no logging call at all "
        "(no `logging.`/`logger.`/`log.<level>(` anywhere in the added lines)",
    ),
    (
        "thread-safety",
        re.compile(r"\bthread[- ]?safe|\bconcurren(t|cy)|\brace condition\b", re.IGNORECASE),
        re.compile(r"\b\w*[lL]ock\s*\(|threading\.|asyncio\.Lock|\bwith\s+\w*lock", re.IGNORECASE),
        "claims thread-safety/concurrency handling, but the diff has no lock or "
        "synchronization primitive at all",
    ),
    (
        "format-validation",
        re.compile(r"\bvalid(ates?|ation)\b.{0,20}\b(email|format|schema|url)\b", re.IGNORECASE),
        re.compile(r"@|re\.(match|search|fullmatch|compile)|\bschema\b|validators?\.", re.IGNORECASE),
        "claims a specific input format is validated (email/url/schema), but the diff "
        "has no pattern-matching or validation construct that could check it",
    ),
]


def _unsupported_behavior_claim(scenario: Scenario, diff: str) -> str | None:
    """Returns the rejection reason if this scenario asserts one of the known
    unsupported-claim shapes, else None."""
    text = f"{scenario.name} {scenario.description}"
    added_lines = "\n".join(
        line[1:] for line in diff.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    for _name, claim_pattern, evidence_pattern, explanation in _UNSUPPORTED_BEHAVIOR_CLAIMS:
        if claim_pattern.search(text) and not evidence_pattern.search(added_lines):
            return explanation
    return None


def validate(scenarios: list[Scenario], diff: str, intent: str) -> list[ValidationResult]:
    evidence = _evidence_terms(diff, intent)
    results = []
    for scenario in scenarios:
        scenario_terms = _terms_of(f"{scenario.name} {scenario.description}")
        specific = scenario_terms - GENERIC_TERMS
        matched = sorted(t for t in specific if _matches(t, evidence))

        unsupported_reason = _unsupported_behavior_claim(scenario, diff)
        if unsupported_reason:
            results.append(
                ValidationResult(
                    scenario=scenario,
                    traceable=False,
                    matched_terms=matched,
                    reason=f"scenario {unsupported_reason} - not supported by the code, regardless of shared terms",
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
