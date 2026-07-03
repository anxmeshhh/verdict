"""
Module 9 - Data Layer (Phase 2).

Postgres mirror of everything the pipeline produces, so any verdict can be
explained from stored data alone - the Phase 2 gate. Plain SQL via psycopg,
no ORM: the queries a human would run to audit a verdict are the same ones
the code runs, nothing hidden behind a query builder.

Ownership model (deliberate):
- The file store (.verdict/runs/*.json, audit.jsonl) stays canonical for the
  plain CLI - it works with zero infrastructure and everything verified in
  Phase 1 keeps working identically.
- When `database_url` is configured, every run record and audit entry is
  ALSO written here (dual-write), and read commands prefer the DB.
- CLI mode degrades honestly: DB unreachable -> one loud stderr warning, the
  file write still succeeds. Server mode (Phase 3) does the opposite and
  refuses new work, per the direction doc - no state, no safe operation.

Tables:
- runs        current state per run (upsert; status can transition in server mode)
- results     per-scenario evidence rows
- audit_log   INSERT-only - the same rows as audit.jsonl, as promised there
- overrides   INSERT-only - run_id + required reason + actor
- jobs        server-mode queue state; UNIQUE dedupe_key is the idempotency guard
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

DATABASE_URL_ENV = "VERDICT_DATABASE_URL"

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id              TEXT PRIMARY KEY,
    created_at          TIMESTAMPTZ NOT NULL,
    status              TEXT NOT NULL,
    model               TEXT,
    intent              TEXT,
    risk_level          TEXT,
    passed              INTEGER,
    failed              INTEGER,
    inconclusive        INTEGER,
    coverage            REAL,
    scenario_source     TEXT,
    scenario_from_cache BOOLEAN,
    failed_stage        TEXT,
    record              JSONB NOT NULL
);
CREATE TABLE IF NOT EXISTS results (
    id            BIGSERIAL PRIMARY KEY,
    run_id        TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    scenario_name TEXT NOT NULL,
    status        TEXT NOT NULL,
    exit_code     INTEGER,
    duration_s    REAL,
    stdout        TEXT,
    stderr        TEXT,
    test_code     TEXT
);
CREATE INDEX IF NOT EXISTS results_run_id_idx ON results(run_id);
CREATE TABLE IF NOT EXISTS audit_log (
    id          BIGSERIAL PRIMARY KEY,
    file_id     INTEGER,
    timestamp   TIMESTAMPTZ NOT NULL,
    actor       TEXT NOT NULL,
    action_type TEXT NOT NULL,
    run_id      TEXT,
    payload     JSONB NOT NULL
);
CREATE TABLE IF NOT EXISTS overrides (
    id         BIGSERIAL PRIMARY KEY,
    run_id     TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    reason     TEXT NOT NULL,
    actor      TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS overrides_run_id_idx ON overrides(run_id);
CREATE TABLE IF NOT EXISTS jobs (
    id         BIGSERIAL PRIMARY KEY,
    dedupe_key TEXT UNIQUE NOT NULL,
    run_id     TEXT,
    status     TEXT NOT NULL,
    params     JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);
"""


class StoreError(Exception):
    """The data layer is configured but unusable - callers decide whether
    that's a warning (CLI mode) or a hard stop (server mode)."""


def resolve_database_url(config=None) -> str:
    """Env var wins over config, same precedence rule as the API key."""
    env = os.environ.get(DATABASE_URL_ENV, "").strip()
    if env:
        return env
    if config is not None:
        return (getattr(config, "database_url", "") or "").strip()
    return ""


def is_configured(config=None) -> bool:
    return bool(resolve_database_url(config))


def connect(database_url: str):
    """One connection per operation - simple, correct, no pooling until a
    measured bottleneck proves the need (anti-vibe-coding rule 10)."""
    try:
        import psycopg
    except ImportError as e:
        raise StoreError(
            "database_url is configured but psycopg is not installed - "
            "run: pip install 'verdict[server]'"
        ) from e
    try:
        return psycopg.connect(database_url, connect_timeout=5)
    except Exception as e:  # psycopg.OperationalError and friends
        raise StoreError(f"cannot connect to Postgres: {e}") from e


def init_schema(database_url: str) -> None:
    with connect(database_url) as conn:
        conn.execute(SCHEMA)


def _warn(message: str) -> None:
    """CLI-mode degradation is loud, never silent - and lands on stderr so
    --json consumers reading stdout are never polluted."""
    print(f"  ! store          {message} (file record still written)", file=sys.stderr)


def save_run_record(database_url: str, record: dict) -> None:
    """Upsert the run row + replace its per-scenario evidence rows."""
    risk = record.get("risk") or {}
    with connect(database_url) as conn:
        conn.execute(
            """
            INSERT INTO runs (run_id, created_at, status, model, intent, risk_level,
                              passed, failed, inconclusive, coverage, scenario_source,
                              scenario_from_cache, failed_stage, record)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (run_id) DO UPDATE SET
                status = EXCLUDED.status, risk_level = EXCLUDED.risk_level,
                passed = EXCLUDED.passed, failed = EXCLUDED.failed,
                inconclusive = EXCLUDED.inconclusive, coverage = EXCLUDED.coverage,
                failed_stage = EXCLUDED.failed_stage, record = EXCLUDED.record
            """,
            (
                record["run_id"],
                record.get("created_at") or datetime.now(timezone.utc).isoformat(),
                record.get("status", "completed"),
                record.get("model"),
                record.get("intent"),
                risk.get("level"),
                risk.get("passed"),
                risk.get("failed"),
                risk.get("inconclusive"),
                risk.get("coverage"),
                record.get("scenario_source"),
                record.get("scenario_from_cache"),
                record.get("failed_stage"),
                json.dumps(record),
            ),
        )
        conn.execute("DELETE FROM results WHERE run_id = %s", (record["run_id"],))
        for r in record.get("results", []):
            conn.execute(
                """
                INSERT INTO results (run_id, scenario_name, status, exit_code,
                                     duration_s, stdout, stderr, test_code)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    record["run_id"],
                    r.get("scenario_name"),
                    r.get("status"),
                    r.get("exit_code"),
                    r.get("duration_s"),
                    r.get("stdout"),
                    r.get("stderr"),
                    r.get("test_code"),
                ),
            )


def save_audit_entry(database_url: str, entry: dict) -> None:
    with connect(database_url) as conn:
        conn.execute(
            """
            INSERT INTO audit_log (file_id, timestamp, actor, action_type, run_id, payload)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                entry.get("id"),
                entry["timestamp"],
                entry["actor"],
                entry["action_type"],
                entry.get("run_id"),
                json.dumps(entry.get("payload") or {}),
            ),
        )


def mirror_run(record: dict, config=None) -> None:
    """Dual-write entry point for CLI mode: best-effort, loud on failure."""
    url = resolve_database_url(config)
    if not url:
        return
    try:
        save_run_record(url, record)
    except StoreError as e:
        _warn(str(e))


def mirror_audit(entry: dict, config=None) -> None:
    url = resolve_database_url(config)
    if not url:
        return
    try:
        save_audit_entry(url, entry)
    except StoreError as e:
        _warn(str(e))


def load_run_record(database_url: str, run_id: str) -> dict | None:
    with connect(database_url) as conn:
        row = conn.execute("SELECT record FROM runs WHERE run_id = %s", (run_id,)).fetchone()
    if row is None:
        return None
    record = row[0] if isinstance(row[0], dict) else json.loads(row[0])
    return record


def list_run_records(database_url: str, limit: int | None = None) -> list[dict]:
    query = "SELECT record FROM runs ORDER BY created_at DESC"
    params: tuple = ()
    if limit:
        query += " LIMIT %s"
        params = (limit,)
    with connect(database_url) as conn:
        rows = conn.execute(query, params).fetchall()
    return [r[0] if isinstance(r[0], dict) else json.loads(r[0]) for r in rows]


def add_override(database_url: str, run_id: str, reason: str, actor: str) -> dict:
    """INSERT-only: an override never edits the run, it annotates it."""
    created_at = datetime.now(timezone.utc).isoformat()
    with connect(database_url) as conn:
        exists = conn.execute("SELECT 1 FROM runs WHERE run_id = %s", (run_id,)).fetchone()
        if exists is None:
            raise StoreError(f"run {run_id} is not in the database - run 'verdict db migrate-files' first?")
        conn.execute(
            "INSERT INTO overrides (run_id, reason, actor, created_at) VALUES (%s, %s, %s, %s)",
            (run_id, reason, actor, created_at),
        )
    return {"run_id": run_id, "reason": reason, "actor": actor, "created_at": created_at}


def get_overrides(database_url: str, run_id: str) -> list[dict]:
    with connect(database_url) as conn:
        rows = conn.execute(
            "SELECT reason, actor, created_at FROM overrides WHERE run_id = %s ORDER BY id",
            (run_id,),
        ).fetchall()
    return [{"reason": r[0], "actor": r[1], "created_at": str(r[2])} for r in rows]


def override_rate(database_url: str) -> dict:
    """Override rate is a first-class product metric from day one (Section 13)."""
    with connect(database_url) as conn:
        total = conn.execute("SELECT count(*) FROM runs WHERE status = 'completed'").fetchone()[0]
        overridden = conn.execute("SELECT count(DISTINCT run_id) FROM overrides").fetchone()[0]
    return {
        "completed_runs": total,
        "overridden_runs": overridden,
        "override_rate": round(overridden / total, 4) if total else None,
    }


def migrate_files(database_url: str, root: Path | None = None) -> dict:
    """Backfill the existing file store into Postgres - the migration
    audit.py's docstring has promised since Phase 1. Idempotent: runs
    upsert by id; audit entries dedupe on their file id."""
    from verdict import audit
    from verdict.reporter import list_runs

    root = root or Path.cwd()
    records = list_runs(root)
    migrated_runs = 0
    for record in records:
        save_run_record(database_url, record)
        migrated_runs += 1

    entries = audit.read_all(root)
    migrated_audit = 0
    with connect(database_url) as conn:
        for entry in entries:
            already = conn.execute(
                "SELECT 1 FROM audit_log WHERE file_id = %s AND timestamp = %s",
                (entry.get("id"), entry["timestamp"]),
            ).fetchone()
            if already:
                continue
            conn.execute(
                """
                INSERT INTO audit_log (file_id, timestamp, actor, action_type, run_id, payload)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    entry.get("id"),
                    entry["timestamp"],
                    entry["actor"],
                    entry["action_type"],
                    entry.get("run_id"),
                    json.dumps(entry.get("payload") or {}),
                ),
            )
            migrated_audit += 1
    return {"runs": migrated_runs, "audit_entries": migrated_audit}


def check(database_url: str, timeout: float = 5.0) -> tuple[bool, str]:
    """Module 18 liveness probe - never faked."""
    try:
        with connect(database_url) as conn:
            conn.execute("SELECT 1")
        return True, "connected"
    except StoreError as e:
        return False, str(e)


# --- Phase 3: server-mode job state (Module 10) --------------------------
# The UNIQUE dedupe_key constraint is the idempotency guard the doc demands
# ("deduped on commit SHA", "no double-runs") - enforced by Postgres itself,
# not by application logic that could race.

def _row_to_job(row) -> dict:
    return {
        "job_id": row[0],
        "dedupe_key": row[1],
        "run_id": row[2],
        "status": row[3],
        "params": row[4] if isinstance(row[4], dict) else json.loads(row[4]),
        "created_at": str(row[5]),
        "updated_at": str(row[6]),
    }


_JOB_COLS = "id, dedupe_key, run_id, status, params, created_at, updated_at"


def create_job(database_url: str, dedupe_key: str, params: dict, run_id: str) -> tuple[dict, bool]:
    """Insert a queued job, or return the existing one for the same dedupe
    key. Returns (job, created). Atomic via ON CONFLICT DO NOTHING - two
    concurrent submits of the same commit can never both insert."""
    now = datetime.now(timezone.utc).isoformat()
    with connect(database_url) as conn:
        inserted = conn.execute(
            """
            INSERT INTO jobs (dedupe_key, run_id, status, params, created_at, updated_at)
            VALUES (%s, %s, 'queued', %s, %s, %s)
            ON CONFLICT (dedupe_key) DO NOTHING
            RETURNING """ + _JOB_COLS,
            (dedupe_key, run_id, json.dumps(params), now, now),
        ).fetchone()
        if inserted is not None:
            return _row_to_job(inserted), True
        existing = conn.execute(
            f"SELECT {_JOB_COLS} FROM jobs WHERE dedupe_key = %s", (dedupe_key,)
        ).fetchone()
        return _row_to_job(existing), False


def get_job(database_url: str, job_id: int) -> dict | None:
    with connect(database_url) as conn:
        row = conn.execute(f"SELECT {_JOB_COLS} FROM jobs WHERE id = %s", (job_id,)).fetchone()
    return _row_to_job(row) if row else None


def update_job(database_url: str, job_id: int, status: str) -> None:
    with connect(database_url) as conn:
        conn.execute(
            "UPDATE jobs SET status = %s, updated_at = %s WHERE id = %s",
            (status, datetime.now(timezone.utc).isoformat(), job_id),
        )


def delete_job(database_url: str, job_id: int) -> None:
    """Only for a job that never got enqueued (e.g. broker down) - a job row
    with no queued task behind it would otherwise dedupe-block forever."""
    with connect(database_url) as conn:
        conn.execute("DELETE FROM jobs WHERE id = %s", (job_id,))


def queue_depth(database_url: str) -> dict:
    """Counts by status - the Section 11 backpressure signal."""
    with connect(database_url) as conn:
        rows = conn.execute("SELECT status, count(*) FROM jobs GROUP BY status").fetchall()
    counts = {status: n for status, n in rows}
    counts["pending_total"] = sum(
        n for s, n in counts.items() if s == "queued" or s.startswith("running") or s == "waiting_on_llm"
    )
    return counts
