"""
Remediation-advisor agent (Verdict Intelligence).

Autonomous, event-triggered: runs on any new HIGH/CRITICAL finding, right
after Triage, no human prompt. Its one job - draft a plausible fix
approach and attach it to the finding record. Always a suggestion, never
applied by anything: this agent has no ability to write to a repo, open a
PR, or touch a single line of code. Getting the suggestion wrong costs
nothing but a bad suggestion - the same reasoning that lets this run
autonomously in the first place.
"""
import re

from verdict import llm, store
from verdict.config import Config

ADVISORY_SEVERITIES = {"HIGH", "CRITICAL"}

PROMPT_TEMPLATE = """A security scanner found this vulnerability:

vuln_class: {vuln_class}
name: {name}
description: {description}
evidence: {evidence}

In 2-4 sentences, suggest a concrete fix approach (e.g. what specific
change - parameterized query, permission check, redaction, safe loader -
would address this). Be specific to the vuln_class, not generic advice.
Plain text only, no markdown, no code block, no preamble like "Here's a
suggestion:".
"""


def _clean(text: str) -> str:
    text = re.sub(r"^<think>.*?</think>\s*", "", text.strip(), flags=re.DOTALL)
    return text.strip().strip("`").strip()


def advise(finding: dict, config: Config, database_url: str) -> str | None:
    """Drafts a suggested fix for a HIGH/CRITICAL finding. Returns None (and
    writes nothing) for anything below that threshold, or if the provider
    call fails - a missing suggestion is just a missing suggestion, never
    treated as evidence of anything about the finding itself."""
    severity = (finding.get("severity") or "").upper()
    if severity not in ADVISORY_SEVERITIES:
        return None

    prompt = PROMPT_TEMPLATE.format(
        vuln_class=finding["vuln_class"],
        name=finding["name"],
        description=finding.get("description") or finding["name"],
        evidence=(finding.get("evidence") or "")[:500],
    )
    try:
        resp = llm.call(prompt, config, json_format=False)
    except llm.LLMDown:
        return None

    suggestion = _clean(resp.text)
    if not suggestion:
        return None

    store.set_suggested_fix(database_url, finding["id"], suggestion)
    return suggestion
