"""
Pulls a real, labeled evaluation set from CVEfixes for Phase 6's precision/
recall gate - same spirit as phase0/'s original 25 hand-pulled {diff, intent}
pairs, just sourced from a real public vulnerability dataset instead of this
project's own commit history, and labeled by real CWE instead of manual
judgment.

CWE -> vuln_class mapping is deliberately narrow (only CWEs that map
unambiguously to one of generator.py's four classes) - a commit whose CWE
doesn't clearly belong to one of these classes is not in scope for this
gate, same "don't guess" discipline as everywhere else in this project.
"""
import json
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "benchmark" / "data" / "CVEfixes.db"
OUT_PATH = Path(__file__).parent / "dataset.json"
SAMPLE_PER_CLASS = 5
MAX_DIFF_CHARS = 20_000  # keep well inside generator.py's own MAX_DIFF_CHARS

CWE_MAP = {
    "injection": ["CWE-89", "CWE-78", "CWE-94"],
    "auth_bypass": ["CWE-287", "CWE-306", "CWE-862", "CWE-863"],
    # CWE-532 ("sensitive info in log file") deliberately dropped - a real
    # eval run surfaced a CWE-532 commit whose actual leak was an
    # uninitialized memory buffer, not a credential/token, which is too broad
    # a match for this project's narrowly-scoped secret_leak vuln_class
    # (credentials/tokens/API keys specifically, per generator.py's prompt).
    "secret_leak": ["CWE-798", "CWE-312"],
    "insecure_deserialization": ["CWE-502"],
}


def extract() -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    examples = []
    for vuln_class, cwes in CWE_MAP.items():
        placeholders = ",".join("?" for _ in cwes)
        cur.execute(
            f"""
            SELECT DISTINCT c.hash, c.msg, c.repo_url
            FROM commits c
            JOIN fixes f ON c.hash = f.hash
            JOIN cwe_classification cwc ON f.cve_id = cwc.cve_id
            WHERE cwc.cwe_id IN ({placeholders})
              AND length(c.msg) > 15
            ORDER BY c.hash
            """,
            cwes,
        )
        rows = cur.fetchall()
        picked = 0
        for row in rows:
            if picked >= SAMPLE_PER_CLASS:
                break
            cur.execute(
                "SELECT diff FROM file_change WHERE hash = ? AND diff IS NOT NULL AND diff != ''",
                (row["hash"],),
            )
            diffs = [r["diff"] for r in cur.fetchall()]
            if not diffs:
                continue
            diff_text = "\n".join(diffs)
            if len(diff_text) > MAX_DIFF_CHARS or len(diff_text) < 50:
                continue  # too large or too small to be a meaningful, fair test
            examples.append(
                {
                    "vuln_class": vuln_class,
                    "hash": row["hash"],
                    "repo_url": row["repo_url"],
                    "intent": row["msg"].strip(),
                    "diff": diff_text,
                }
            )
            picked += 1
    conn.close()
    return examples


if __name__ == "__main__":
    examples = extract()
    OUT_PATH.write_text(json.dumps(examples, indent=2), encoding="utf-8")
    counts = {}
    for e in examples:
        counts[e["vuln_class"]] = counts.get(e["vuln_class"], 0) + 1
    print(f"wrote {len(examples)} examples to {OUT_PATH}: {counts}")
