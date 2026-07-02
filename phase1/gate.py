"""
Phase 1 gate (Section 12): "Runs cleanly end-to-end on your own repo,
10 times in a row, no crashes."

Stricter reading used here: 10 different real commits from this repo's own
history, one run each. A crash is an unhandled traceback. A handled abort
(recorded errored/skipped run) is not a crash but is also not end-to-end -
both are reported separately, with no massaging.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).parent.parent
RESULTS_FILE = Path(__file__).parent / "gate_results.json"

# One run per real commit, oldest to newest
REFS = [
    "d420fc5",  # Module 1: Config & Setup
    "fcfcb0d",  # Module 2: Intent Extractor
    "203bc22",  # Module 3a: Scenario Generator
    "be95219",  # Module 3b: Scenario Authoring
    "eca76a0",  # Module 4: Scenario Validator
    "b322d43",  # Module 5: Sandbox Runner
    "36b0a91",  # Module 6: Risk Scorer
    "121551f",  # Module 7: Reporter
    "7f6df4d",  # Module 8: CLI
    "e2fd5c6",  # Pre-gate hardening
]


def latest_run_record() -> dict | None:
    runs = sorted((REPO / ".verdict" / "runs").glob("run_*.json"), key=lambda p: p.stat().st_mtime)
    if not runs:
        return None
    return json.loads(runs[-1].read_text(encoding="utf-8"))


def main() -> None:
    outcomes = []
    for i, ref in enumerate(REFS, 1):
        print(f"[{i}/10] verdict run --ref {ref} ...", flush=True)
        start = time.monotonic()
        proc = subprocess.run(
            [sys.executable, "-m", "verdict.cli", "run", "--ref", ref, "--max-scenarios", "2", "--timeout", "180"],
            cwd=REPO,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        duration = round(time.monotonic() - start, 1)
        output = proc.stdout + proc.stderr
        crashed = "Traceback (most recent call last)" in output
        record = latest_run_record()
        status = record.get("status", "missing") if record else "missing"
        risk = (record.get("risk") or {}).get("level") if record and status == "completed" else None

        outcome = {
            "ref": ref,
            "exit_code": proc.returncode,
            "crashed": crashed,
            "record_status": status,
            "risk": risk,
            "duration_s": duration,
            "run_id": record.get("run_id") if record else None,
            "tokens": record.get("tokens") if record else None,
        }
        if crashed:
            # keep the evidence - a crash we can't diagnose is a crash we refix blind
            outcome["crash_output_tail"] = output[-3000:]
        outcomes.append(outcome)
        tag = "CRASH" if crashed else status.upper()
        print(f"        -> {tag}" + (f" ({risk})" if risk else "") + f"  exit={proc.returncode}  {duration}s", flush=True)

    crashes = sum(1 for o in outcomes if o["crashed"])
    completed = sum(1 for o in outcomes if o["record_status"] == "completed" and not o["crashed"])
    aborted = sum(1 for o in outcomes if o["record_status"] in ("errored", "skipped") and not o["crashed"])

    summary = {
        "total": len(outcomes),
        "completed_verdicts": completed,
        "handled_aborts": aborted,
        "crashes": crashes,
        "gate": "PASS" if crashes == 0 and completed == len(outcomes) else "FAIL",
        "outcomes": outcomes,
    }
    RESULTS_FILE.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(f"\ncompleted verdicts: {completed}/10")
    print(f"handled aborts:     {aborted}/10")
    print(f"crashes:            {crashes}/10")
    print(f"\nGATE: {summary['gate']}  (evidence: {RESULTS_FILE})")


if __name__ == "__main__":
    main()
