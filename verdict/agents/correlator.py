"""
Correlator agent (Verdict Intelligence).

Autonomous and event-triggered: runs right after a new finding is saved by
the Phase 6 pipeline, not on request. Its one job - does this new finding
match a past one (same underlying vulnerability, different service/run) so
a team gets told "we've seen this before" instead of rediscovering it from
zero every time.

Bounded like every other LLM step in this project: the model picks a
candidate ID from a list Verdict Intelligence already fetched itself. It
never invents a match, and a returned ID that isn't actually in the
candidate list is discarded, not trusted - the same "don't guess" rule
generator.py already applies to scenario proposals.
"""
import json
import re
from dataclasses import dataclass

from verdict import llm, store
from verdict.config import Config

MAX_CANDIDATES = 15

PROMPT_TEMPLATE = """You are correlating security findings across a codebase's history.

New finding:
  vuln_class: {vuln_class}
  name: {name}
  description: {description}
  repo: {repo_name}

Candidate past findings (same vuln_class, different runs):
{candidates}

Does the new finding represent the SAME underlying vulnerability as any one
of the candidates - same root cause (e.g. a shared library bug reintroduced
elsewhere, or a copy-pasted pattern) - not just the same general category?
Respond with ONLY valid JSON, no other text:

{{"match_id": <the candidate's id, or null if none genuinely match>, "reason": "one sentence, or empty string if no match"}}
"""


@dataclass
class CorrelationResult:
    finding_id: int
    matched_finding_id: int
    reason: str


def _extract_json(text: str) -> str:
    text = re.sub(r"^<think>.*?</think>\s*", "", text.strip(), flags=re.DOTALL)
    match = re.match(r"^```(?:json)?\s*\n(.*?)\n?```\s*$", text.strip(), re.DOTALL)
    if match:
        text = match.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        text = text[start : end + 1]
    return text


def _format_candidates(candidates: list[dict]) -> str:
    return "\n".join(
        f'- id={c["id"]}: "{c["name"]}" ({c.get("repo_name") or "unknown"}, {c["created_at"]}) '
        f'- {c.get("description") or c["name"]}'
        for c in candidates
    )


def correlate(finding: dict, config: Config, database_url: str) -> CorrelationResult | None:
    """Looks for a past finding matching this new one. Returns None when
    there's nothing to compare against, the provider is unreachable, the
    response is unparseable, or the model's answer is 'no match' - all of
    these are just 'nothing to report', not errors."""
    candidates = store.list_findings(
        database_url,
        vuln_class=finding["vuln_class"],
        exclude_run_id=finding["run_id"],
        limit=MAX_CANDIDATES,
    )
    if not candidates:
        return None

    prompt = PROMPT_TEMPLATE.format(
        vuln_class=finding["vuln_class"],
        name=finding["name"],
        description=finding.get("description") or finding["name"],
        repo_name=finding.get("repo_name") or "unknown",
        candidates=_format_candidates(candidates),
    )
    try:
        resp = llm.call(prompt, config, json_format=True)
    except llm.LLMDown:
        return None  # a provider hiccup is not evidence of anything - just skip this time

    try:
        parsed = json.loads(_extract_json(resp.text))
        match_id = parsed.get("match_id")
        reason = str(parsed.get("reason") or "").strip()
    except (json.JSONDecodeError, AttributeError, TypeError):
        return None

    if match_id is None:
        return None
    valid_ids = {c["id"] for c in candidates}
    if match_id not in valid_ids:
        return None  # hallucinated id - discard rather than trust, same rule as generator.py's vuln_class guard

    store.set_correlation(database_url, finding["id"], match_id)
    return CorrelationResult(finding_id=finding["id"], matched_finding_id=match_id, reason=reason)
