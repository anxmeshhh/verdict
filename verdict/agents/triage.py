"""
Triage agent (Verdict Intelligence).

Autonomous, event-triggered: runs right after the Correlator, on every new
finding, no human prompt needed. Its one job - decide whether this finding
is alert-worthy, and if so, surface it immediately (console + a durable
local alert log). Purely deterministic ranking logic; no LLM call needed
here - severity and recurrence are already-known facts by this point, not
something to ask a model to judge.

A finding correlated with a past one (the Correlator's job, run just before
this) is always alert-worthy regardless of its own severity - a vulnerability
class reappearing across services is a worse signal than a first
occurrence, even if the individual finding looks routine on its own.
"""
import json
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from verdict import store

ALWAYS_ALERT_SEVERITIES = {"HIGH", "CRITICAL"}


@dataclass
class Alert:
    finding_id: int
    repo_name: str
    vuln_class: str
    name: str
    severity: str
    reason: str  # why this got alerted - severity, recurrence, or both
    recurrence_of: int | None  # matched finding id, if this is a repeat


def _alerts_path(repo: Path) -> Path:
    return repo / ".verdict" / "alerts.jsonl"


def _should_alert(finding: dict) -> str | None:
    """Returns the alert reason, or None if this finding doesn't rise to
    alert-worthy. Deterministic - no ambiguity to resolve, nothing to guess."""
    severity = (finding.get("severity") or "").upper()
    is_recurrence = finding.get("correlated_with") is not None
    if is_recurrence and severity in ALWAYS_ALERT_SEVERITIES:
        return "high severity, and a repeat of a past finding elsewhere"
    if is_recurrence:
        return "recurrence of a past finding elsewhere - escalated regardless of nominal severity"
    if severity in ALWAYS_ALERT_SEVERITIES:
        return f"{severity} severity"
    return None


def triage(finding: dict, repo: Path, database_url: str | None = None) -> Alert | None:
    reason = _should_alert(finding)
    if reason is None:
        return None

    alert = Alert(
        finding_id=finding["id"],
        repo_name=finding.get("repo_name") or "unknown",
        vuln_class=finding["vuln_class"],
        name=finding["name"],
        severity=finding.get("severity") or "UNKNOWN",
        reason=reason,
        recurrence_of=finding.get("correlated_with"),
    )

    # Console alert - printed immediately, not buffered for a report someone
    # has to go looking for. Plain ASCII only - a Windows cp1252 console
    # mangling ⚠/— has already bitten this project once (generated test
    # output through the same kind of pipe), not worth repeating here.
    print(
        f"\n!! VERDICT INTELLIGENCE ALERT - {alert.vuln_class} in {alert.repo_name}: "
        f"{alert.name} ({alert.reason})",
        file=sys.stderr,
    )

    # Durable local log - same append-only pattern as audit.jsonl.
    try:
        path = _alerts_path(repo)
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {**asdict(alert), "timestamp": time.time()}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass  # the console alert already fired - a log write failure isn't a reason to hide that

    if database_url:
        try:
            store.set_finding_status(database_url, finding["id"], "alerted")
        except store.StoreError:
            pass

    return alert
