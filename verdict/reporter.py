"""
Module 7 - Reporter.

Input:  everything the pipeline produced (intent, scenarios, results, risk)
Output: formatted terminal text or JSON, plus a saved run record under
        .verdict/runs/<run_id>.json so every verdict stays auditable.
        (File store is Phase 1 scope; Postgres takes over in Phase 2.)
"""
import json
import secrets
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from verdict.generator import GenerationResult
from verdict.intent import IntentResult
from verdict.sandbox import SandboxResult
from verdict.scorer import RiskReport

_STATUS_TAGS = {
    "passed": "PASSED ",
    "failed": "FAILED ",
    "uncertain": "UNCLEAR",
    "error": "BADTEST",
    "timeout": "TIMEOUT",
}


def new_run_id() -> str:
    return f"run_{secrets.token_hex(3)}"


def build_record(
    run_id: str,
    intent_result: IntentResult,
    generation: GenerationResult,
    results: list[SandboxResult],
    risk: RiskReport,
    model: str,
    tokens: dict | None = None,
) -> dict:
    return {
        "run_id": run_id,
        "status": "completed",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "intent": intent_result.intent,
        "vague": intent_result.vague,
        "diff_lines": intent_result.diff.count("\n"),
        "diff": intent_result.diff,
        "scenario_source": generation.source,
        "generation_prompt": generation.prompt,
        "generation_raw_response": generation.raw_response,
        "results": [asdict(r) for r in results],
        "risk": asdict(risk),
        "tokens": tokens or {},
    }


def build_incomplete_record(
    run_id: str,
    status: str,  # "errored" | "skipped"
    stage: str,
    reason: str,
    model: str,
    intent_result: IntentResult | None = None,
    tokens: dict | None = None,
) -> dict:
    """A run that never reached a verdict still leaves evidence. An errored
    or skipped run that vanishes is a hole in the audit trail."""
    record = {
        "run_id": run_id,
        "status": status,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "failed_stage": stage,
        "reason": reason,
        "tokens": tokens or {},
    }
    if intent_result is not None:
        record["intent"] = intent_result.intent
        record["vague"] = intent_result.vague
        record["diff_lines"] = intent_result.diff.count("\n")
        record["diff"] = intent_result.diff
    return record


def save_run(record: dict, root: Path | None = None) -> Path:
    runs_dir = (root or Path.cwd()) / ".verdict" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    path = runs_dir / f"{record['run_id']}.json"
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return path


def load_run(run_id: str, root: Path | None = None) -> dict | None:
    path = (root or Path.cwd()) / ".verdict" / "runs" / f"{run_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def format_terminal(record: dict) -> str:
    risk = record["risk"]
    lines = []
    coverage = risk["coverage"]
    coverage_txt = f" - coverage {coverage:.0%}" if coverage is not None else ""
    lines.append(f"{risk['level']} RISK - {risk['passed']}/{risk['passed'] + risk['failed']} conclusive passed{coverage_txt}")
    lines.append("")
    for r in record["results"]:
        tag = _STATUS_TAGS.get(r["status"], r["status"].upper())
        first_line = (r["stdout"].strip().splitlines() or [""])[0]
        lines.append(f"  {tag} {r['scenario_name']} ({r['duration_s']}s)")
        if first_line:
            lines.append(f"          {first_line[:100]}")
    lines.append("")
    for reason in risk["reasons"]:
        lines.append(f"  {reason}")
    lines.append("")
    lines.append(f"Run ID: {record['run_id']}   [full evidence: verdict logs {record['run_id']}]")
    return "\n".join(lines)


def format_json(record: dict) -> str:
    """Machine-readable output - everything except the bulky audit fields."""
    slim = {k: v for k, v in record.items() if k not in ("diff", "generation_prompt", "generation_raw_response")}
    for r in slim["results"]:
        r.pop("test_code", None)
    return json.dumps(slim, indent=2)
