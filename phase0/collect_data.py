"""
Phase 0 - Step 1: collect real {diff, intent} pairs from SyncBeats commit history.
No infra, no LLM here - just pulling real data per the doc's Phase 0 gate (Section 12).
"""
import json
import subprocess
from pathlib import Path

REPO_DIR = Path(__file__).parent / "repo"
OUT_FILE = Path(__file__).parent / "dataset.json"
TARGET_COUNT = 25
MAX_DIFF_LINES = 400
MIN_MESSAGE_LEN = 15

SKIP_PREFIXES = ("merge ", "chore: sync", "build:", "the final one")


def run(*args):
    return subprocess.run(
        ["git", *args],
        cwd=REPO_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    ).stdout


def is_real_intent(message: str) -> bool:
    msg = message.strip().lower()
    if len(msg) < MIN_MESSAGE_LEN:
        return False
    if msg.startswith(SKIP_PREFIXES):
        return False
    if msg.startswith("docs:") or msg.startswith("update readme"):
        return False
    return True


def get_diff(sha: str) -> str:
    return run("show", sha, "--format=", "--no-color")


def main():
    log = run("log", "--format=%H|||%s", "--no-merges")
    candidates = []
    for line in log.splitlines():
        sha, _, message = line.partition("|||")
        if is_real_intent(message):
            candidates.append((sha.strip(), message.strip()))

    print(f"Found {len(candidates)} candidate commits with real intent messages")

    dataset = []
    for sha, message in candidates:
        if len(dataset) >= TARGET_COUNT:
            break
        diff = get_diff(sha)
        diff_lines = diff.count("\n")
        if diff_lines == 0 or diff_lines > MAX_DIFF_LINES:
            continue
        dataset.append(
            {
                "id": sha[:8],
                "intent": message,
                "diff": diff,
            }
        )

    OUT_FILE.write_text(json.dumps(dataset, indent=2), encoding="utf-8")
    print(f"Wrote {len(dataset)} {{diff, intent}} pairs to {OUT_FILE}")


if __name__ == "__main__":
    main()
