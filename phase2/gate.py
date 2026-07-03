"""
Phase 2 gate (Section 12): "Can answer 'why did it flag this' for any run,
from stored data alone."

Method: migrate the REAL run history from the live demo repo (the session's
actual verification runs, including the ones that caught the planted
rate-limiter bug) into Postgres, then reconstruct the full "why" for a
flagged run using ONLY SQL queries - no file reads. Plus: dual-write proof
(a fresh save lands in file AND DB) and an override round-trip.

Usage:
    docker run -d --name verdict-postgres -e POSTGRES_USER=verdict \
        -e POSTGRES_PASSWORD=verdict -e POSTGRES_DB=verdict -p 5433:5432 postgres:16-alpine
    python phase2/gate.py [demo_repo_path]

Writes phase2/gate_results.json - the phase evidence, same pattern as
phase0/ and phase1/.
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from verdict import store  # noqa: E402

DEFAULT_URL = "postgresql://verdict:verdict@localhost:5433/verdict"
DEFAULT_DEMO_REPO = r"C:\Users\Animesh\Desktop\Rate Limiter Test"

RESULTS_FILE = Path(__file__).parent / "gate_results.json"


def main() -> int:
    url = os.environ.get("VERDICT_DATABASE_URL", DEFAULT_URL)
    demo_repo = Path(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DEMO_REPO)
    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  - {detail}" if detail else ""))

    store.init_schema(url)
    check("schema_init_idempotent", True, "init_schema ran twice without error" if _reinit(url) else "")

    # --- 1. migrate the real demo-repo history ---------------------------
    counts = store.migrate_files(url, demo_repo)
    check(
        "migrate_real_history",
        counts["runs"] > 0,
        f"{counts['runs']} real runs + {counts['audit_entries']} audit entries from {demo_repo.name}",
    )

    # --- 2. the gate itself: explain a flagged run from SQL alone --------
    import psycopg

    with psycopg.connect(url) as conn:
        flagged = conn.execute(
            """
            SELECT run_id, risk_level, model, intent, record->'risk'->'reasons'
            FROM runs
            WHERE status = 'completed' AND risk_level IN ('MEDIUM', 'HIGH')
            ORDER BY created_at DESC LIMIT 1
            """
        ).fetchone()
        check("found_flagged_run", flagged is not None, f"run {flagged[0]} ({flagged[1]})" if flagged else "no flagged run in history")
        if flagged is None:
            return _finish(checks)

        run_id, risk_level, model, intent, reasons = flagged
        why = {
            "run_id": run_id,
            "risk_level": risk_level,
            "model": model,
            "intent": (intent or "").splitlines()[0] if intent else None,
            "reasons": reasons,
        }
        check("why_has_model_and_intent", bool(model and intent), f"model={model}")
        check("why_has_reasons", bool(reasons), f"{len(reasons or [])} reason line(s)")

        failed_rows = conn.execute(
            """
            SELECT scenario_name, exit_code, stdout, test_code
            FROM results WHERE run_id = %s AND status = 'failed'
            """,
            (run_id,),
        ).fetchall()
        check("failed_scenarios_have_evidence", all(r[2] and r[3] for r in failed_rows) and bool(failed_rows),
              f"{len(failed_rows)} failed scenario(s), each with stdout evidence + full test code")
        why["failed_scenarios"] = [
            {"name": r[0], "exit_code": r[1], "evidence_first_line": (r[2] or "").splitlines()[0][:100]}
            for r in failed_rows
        ]

        prompt_row = conn.execute(
            "SELECT record->>'generation_prompt', record->>'generation_raw_response' FROM runs WHERE run_id = %s",
            (run_id,),
        ).fetchone()
        check("audit_trail_prompt_and_raw_response", bool(prompt_row[0]) and prompt_row[1] is not None,
              f"prompt {len(prompt_row[0] or '')} chars stored")

        audit_rows = conn.execute(
            "SELECT action_type FROM audit_log WHERE run_id = %s ORDER BY id", (run_id,)
        ).fetchall()
        check("audit_log_records_run_lifecycle", len(audit_rows) >= 1,
              f"actions: {[a[0] for a in audit_rows]}")

    # --- 3. override round-trip ------------------------------------------
    override = store.add_override(url, run_id, "phase2 gate: verified override flow", "gate-script")
    fetched = store.get_overrides(url, run_id)
    check("override_roundtrip", any(o["reason"] == override["reason"] for o in fetched))
    rate = store.override_rate(url)
    check("override_rate_metric", rate["override_rate"] is not None,
          f"{rate['overridden_runs']}/{rate['completed_runs']} = {rate['override_rate']}")

    # --- 4. dual-write: a fresh save lands in file AND DB ----------------
    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        (tmp_root / ".verdict").mkdir()
        (tmp_root / ".verdict" / "config.json").write_text(
            json.dumps({"database_url": url}), encoding="utf-8"
        )
        fresh = {
            "run_id": "run_gate_dw",
            "status": "completed",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "model": "gate/dual-write",
            "intent": "dual-write proof",
            "risk": {"level": "LOW", "passed": 1, "failed": 0, "inconclusive": 0, "coverage": 1.0, "reasons": []},
            "results": [],
            "tokens": {},
        }
        from verdict.reporter import save_run

        path = save_run(fresh, tmp_root)
        in_db = store.load_run_record(url, "run_gate_dw")
        check("dual_write_file_and_db", path.exists() and in_db is not None,
              "one save_run() call produced both the file record and the DB row")

    return _finish(checks)


def _reinit(url: str) -> bool:
    store.init_schema(url)
    return True


def _finish(checks: list[dict]) -> int:
    passed = sum(1 for c in checks if c["ok"])
    result = {
        "gate": "Phase 2 - answer 'why did it flag this' from stored data alone",
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "checks": checks,
        "passed": passed,
        "total": len(checks),
        "gate_met": passed == len(checks),
    }
    RESULTS_FILE.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print()
    print(f"{passed}/{len(checks)} checks passed -> {'GATE MET' if result['gate_met'] else 'GATE NOT MET'}")
    print(f"evidence: {RESULTS_FILE}")
    return 0 if result["gate_met"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
