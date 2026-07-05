"""
Agent activity stream - a live, append-only record of what the autonomous
agents actually did, as they did it.

The agents run on a background thread (see verdict/findings.py::save), so
their work happens after the verdict is already on screen. This stream is
how you watch it anyway: each agent appends one line here the moment it
acts, and `verdict agents [--follow]` renders it. Same append-only,
one-JSON-object-per-line shape as audit.jsonl - deliberately its own file
so tailing agent activity never has to wade through run/config audit noise.

Best-effort by design: a failed write here must never affect an agent's
actual work or a finding that already saved. An agent that can't log what
it did is a cosmetic loss, not a correctness one.
"""
import json
import time
import unicodedata
from pathlib import Path

EVENTS_FILENAME = "agent_events.jsonl"

# Common unicode punctuation an LLM emits that a legacy Windows (cp1252)
# console chokes on - normalized to ASCII so a stored event renders anywhere,
# same defensive stance the Triage alert already takes. Anything left
# non-encodable after this is dropped rather than crashing the feed.
_PUNCT_MAP = {
    "‑": "-", "–": "-", "—": "-", "‘": "'", "’": "'",
    "“": '"', "”": '"', "…": "...", " ": " ",
}


def _ascii_safe(text: str) -> str:
    for bad, good in _PUNCT_MAP.items():
        text = text.replace(bad, good)
    # strip any remaining non-latin-1 chars the map didn't cover
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")

# The agents that can emit here, and a short glyph/label the renderer uses.
AGENTS = {
    "correlator": "CORRELATOR",
    "triage": "TRIAGE",
    "remediation": "REMEDIATION",
    "verifier": "VERIFIER",
}


def _events_path(repo: Path) -> Path:
    return repo / ".verdict" / EVENTS_FILENAME


def emit(repo: Path, agent: str, action: str, detail: str = "", finding_id: int | None = None) -> None:
    """Append one agent-activity line. Never raises - logging activity must
    not be able to break the activity itself."""
    try:
        path = _events_path(repo)
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": time.time(),
            "agent": agent,
            "action": action,
            "detail": _ascii_safe(detail),
            "finding_id": finding_id,
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def read_recent(repo: Path, limit: int = 50) -> list[dict]:
    path = _events_path(repo)
    if not path.exists():
        return []
    lines = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    lines.append(json.loads(line))
    except (OSError, json.JSONDecodeError):
        return []
    return lines[-limit:]
