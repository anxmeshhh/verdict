"""
Module: Finding extraction (Phase 6).

Turns this run's evidence into structured Finding records - the local schema
Phase 7's BigQuery vulnerability_map will later ingest wholesale. Two
sources, both already-computed facts, nothing generated here:

- A security-tagged scenario that actually FAILED in the sandbox (post the
  confirm-FAILED pass, so a flaky generated test can't produce a false
  finding) - the vulnerability was demonstrated, not just proposed.
- verdict/depcheck.py's deterministic OSV.dev lookups.

Pure deterministic logic - no LLM call happens in this file.
"""
import json
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


def save(findings: list[Finding], repo: Path, run_id: str) -> Path | None:
    """Writes .verdict/findings/<run_id>.json - the local record Phase 7's
    BigQuery vulnerability_map ingests from later. Returns None (and never
    raises) when there's nothing to write or the write fails - saving
    findings is not something that should ever break a run whose actual
    verdict already completed."""
    if not findings:
        return None
    directory = _findings_dir(repo)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{run_id}.json"
        payload = {
            "run_id": run_id,
            "generated_at": time.time(),
            "findings": [asdict(f) for f in findings],
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path
    except OSError:
        return None
