"""
Append-only audit log - what happened, in order, permanently.

Results answer "what was the verdict"; the audit log answers "what actions
occurred". One JSON object per line in .verdict/audit.jsonl, only ever
appended. Corrections are new entries referencing the old one - history is
never rewritten. This file store is Phase 1 scope; the same rows migrate
into an INSERT-only Postgres table in Phase 2.
"""
import getpass
import json
from datetime import datetime, timezone
from pathlib import Path

AUDIT_FILENAME = "audit.jsonl"

ACTION_TYPES = (
    "run_started",
    "run_completed",
    "run_errored",
    "run_skipped",
    "run_overridden",
    "config_change",
    "health_check",
    "hook_installed",
    "hook_removed",
    "db_migrated",
)


def _audit_path(root: Path | None = None) -> Path:
    return (root or Path.cwd()) / ".verdict" / AUDIT_FILENAME


def _actor() -> str:
    try:
        return f"user:{getpass.getuser()}"
    except OSError:
        return "user:unknown"


def append(
    action_type: str,
    payload: dict,
    run_id: str | None = None,
    root: Path | None = None,
    actor: str | None = None,
) -> dict:
    """Append one immutable entry. Returns the entry as written."""
    if action_type not in ACTION_TYPES:
        raise ValueError(f"unknown action_type '{action_type}' - add it to ACTION_TYPES deliberately")
    path = _audit_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)

    last_id = 0
    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    last_id = json.loads(line)["id"]

    entry = {
        "id": last_id + 1,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "actor": actor or _actor(),
        "action_type": action_type,
        "run_id": run_id,
        "payload": payload,
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")

    # Phase 2 dual-write: mirror into Postgres when configured. The file
    # write above already succeeded - a DB failure warns loudly (stderr)
    # but never breaks the command. Local import avoids a cycle.
    from verdict import store
    from verdict.config import load_config

    store.mirror_audit(entry, load_config(root))
    return entry


def read_all(root: Path | None = None) -> list[dict]:
    path = _audit_path(root)
    if not path.exists():
        return []
    entries = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                entries.append(json.loads(line))
    return entries
