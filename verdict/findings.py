"""
Module: Finding extraction + agent dispatch (Phase 6 / Verdict Intelligence).

Turns this run's evidence into structured Finding records. Two sources,
both already-computed facts, nothing generated here:

- A security-tagged scenario that actually FAILED in the sandbox (post the
  confirm-FAILED pass, so a flaky generated test can't produce a false
  finding) - the vulnerability was demonstrated, not just proposed.
- verdict/depcheck.py's deterministic OSV.dev lookups.

Finding EXTRACTION is pure deterministic logic. save() additionally
dispatches the autonomous agent chain (verdict/agents/*), which does make
LLM calls - but on a background thread, so it never blocks the verdict the
user is waiting for. See save() and run_agents().
"""
import json
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from verdict import depcheck
from verdict.generator import Scenario
from verdict.sandbox import SandboxResult

# A demonstrated security-scenario failure is always treated as HIGH - the
# sandbox actually proved the vulnerable behavior, not just flagged a
# suspicion. Dependency findings instead carry whatever severity OSV.dev
# itself reports, since that's a real, external, independently-assessed
# rating rather than something Verdict is asserting on its own.
DEMONSTRATED_SEVERITY = "HIGH"


@dataclass
class Finding:
    vuln_class: str
    name: str
    description: str
    severity: str
    evidence: str
    source: str  # "scenario" | "dependency"


def _findings_dir(repo: Path) -> Path:
    return repo / ".verdict" / "findings"


def from_scenario_results(
    results: list[SandboxResult], scenarios: list[Scenario]
) -> list[Finding]:
    """Only scenarios tagged with a vuln_class AND that actually FAILED
    become findings - a passed security scenario means the check held, and
    an uncertain/error result is non-evidence, same rule score() already
    applies to risk in general."""
    by_name = {s.name: s for s in scenarios}
    findings = []
    for result in results:
        if result.status != "failed":
            continue
        scenario = by_name.get(result.scenario_name)
        if scenario is None or not scenario.vuln_class:
            continue
        evidence = (result.stdout or result.stderr or "").strip()[-500:]
        findings.append(
            Finding(
                vuln_class=scenario.vuln_class,
                name=scenario.name,
                description=scenario.description,
                severity=DEMONSTRATED_SEVERITY,
                evidence=evidence or f"scenario '{scenario.name}' failed (exit {result.exit_code})",
                source="scenario",
            )
        )
    return findings


def from_dependencies(repo: Path) -> list[Finding]:
    """Best-effort: a dependency-check transport failure must not break a
    run that otherwise completed - same principle as everywhere else in this
    pipeline (a broken checker is not evidence against the code)."""
    try:
        dep_findings = depcheck.check_dependencies(repo)
    except depcheck.DepCheckError:
        return []
    return [
        Finding(
            vuln_class="dependency_cve",
            name=f"{f.dependency.name}=={f.dependency.version}",
            description=f.summary or f.vuln_id,
            severity=f.severity or "UNKNOWN",
            evidence=f.vuln_id,
            source="dependency",
        )
        for f in dep_findings
    ]


def extract(
    results: list[SandboxResult], scenarios: list[Scenario], repo: Path
) -> list[Finding]:
    return from_scenario_results(results, scenarios) + from_dependencies(repo)


def run_agents(inserted: list[dict], repo: Path, config, database_url: str) -> None:
    """The autonomous four-agent chain, per finding, in order: Correlator ->
    Verification-requester (only if the Correlator matched) -> Triage ->
    Remediation-advisor. Every agent is wrapped defensively - a provider
    hiccup or one agent failing must never stop the others or affect the
    finding that already saved. Safe to call on its own thread: every store
    call opens its own connection (no shared DB handle), and none of these
    agents ever mutates the finding's verdict, only its metadata.

    None of this is something a human has to ask for - that's the whole
    point of the layer - but it also must never be the reason a user waits
    for their verdict (see save(), which runs this in the background)."""
    from verdict.agents import correlator, remediation, triage, verifier

    for row in inserted:
        if config is not None:
            try:
                result = correlator.correlate(row, config, database_url)
                if result is not None:
                    row["correlated_with"] = result.matched_finding_id
                    # Verification-requester: the match itself is the
                    # trigger - flags the OLDER finding, not this one.
                    verifier.request_reverification(result.matched_finding_id, row, database_url)
            except Exception:
                pass
        try:
            triage.triage(row, repo, database_url=database_url)
        except Exception:
            pass
        if config is not None:
            try:
                remediation.advise(row, config, database_url)
            except Exception:
                continue


def save(
    findings: list[Finding],
    repo: Path,
    run_id: str,
    config=None,
    repo_name: str | None = None,
    agents_background: bool = True,
) -> Path | None:
    """Writes .verdict/findings/<run_id>.json - stays canonical, same rule as
    runs/audit_log elsewhere in this project. When database_url is
    configured, also mirrors into Postgres (verdict/store.py's findings
    table) - the queryable layer the agent layer and the UI read from - and
    then fires the autonomous agent chain.

    The agent chain makes 1-2 LLM calls per HIGH finding, so by default it
    runs on a background thread: the user gets their verdict immediately,
    agents work behind it. The thread is non-daemon, so a short-lived CLI
    process still waits for the agents to finish before it fully exits
    (nothing gets killed mid-correlation) - the user just isn't blocked on
    them to SEE the verdict. Pass agents_background=False for deterministic,
    inline execution (tests, or a caller that wants agents done before it
    returns).

    Returns the local path (or None if there was nothing to write, or the
    local write itself failed) - saving findings must never break a run
    whose actual verdict already completed, and the Postgres mirror + agents
    are best-effort on top of that, never the other way around."""
    if not findings:
        return None
    directory = _findings_dir(repo)
    path = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{run_id}.json"
        payload = {
            "run_id": run_id,
            "generated_at": time.time(),
            "findings": [asdict(f) for f in findings],
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        path = None

    from verdict import store

    url = store.resolve_database_url(config)
    if url:
        try:
            inserted = store.save_findings(url, run_id, repo_name or repo.name, [asdict(f) for f in findings])
        except store.StoreError as e:
            store._warn(f"findings: {e}")
            inserted = []
        if inserted:
            if agents_background:
                threading.Thread(
                    target=run_agents,
                    args=(inserted, repo, config, url),
                    name="verdict-agents",
                    daemon=False,
                ).start()
            else:
                run_agents(inserted, repo, config, url)

    return path
