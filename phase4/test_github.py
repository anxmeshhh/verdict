"""
Phase 4 verification: everything testable without a live GitHub repo.

The full gate ("a real PR gets an accurate check, unaided") runs on GitHub
via action/ - see phase4/README.md for the one manual step (repo secret).
This script proves every piece below that: HMAC verification, event parsing,
webhook -> job flow, clone/checkout correctness, conclusion mapping, the
markdown body, and the exact Checks API payload.

Prereqs: verdict-postgres on :5433 (same as phase2/3 gates).
Writes phase4/test_results.json.
"""
import hashlib
import hmac
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DATABASE_URL = os.environ.get("VERDICT_DATABASE_URL", "postgresql://verdict:verdict@localhost:5433/verdict")
SECRET = "gate-webhook-secret"
RESULTS_FILE = Path(__file__).parent / "test_results.json"

checks: list[dict] = []


def check(name, ok, detail=""):
    checks.append({"check": name, "ok": bool(ok), "detail": detail})
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  - {detail}" if detail else ""))


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def make_origin_repo() -> tuple[Path, str, str]:
    """A local 'origin' with a base commit and a PR-head commit."""
    d = Path(tempfile.mkdtemp()) / "origin"
    d.mkdir(parents=True)
    kw = dict(cwd=d, capture_output=True, text=True)
    subprocess.run(["git", "init", "-q"], **kw)
    subprocess.run(["git", "config", "user.email", "t@t.t"], **kw)
    subprocess.run(["git", "config", "user.name", "t"], **kw)
    (d / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], **kw)
    subprocess.run(["git", "commit", "-q", "-m", "base"], **kw)
    base_sha = subprocess.run(["git", "rev-parse", "HEAD"], **kw).stdout.strip()
    (d / "calc.py").write_text("def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], **kw)
    subprocess.run(["git", "commit", "-q", "-m", "add sub function"], **kw)
    head_sha = subprocess.run(["git", "rev-parse", "HEAD"], **kw).stdout.strip()
    return d, base_sha, head_sha


def main() -> int:
    from verdict.server import github as gh

    # --- 1. HMAC verification ---------------------------------------------
    body = b'{"hello": "world"}'
    check("signature_valid_accepted", gh.verify_signature(body, sign(body), SECRET))
    check("signature_tampered_rejected", not gh.verify_signature(body + b"x", sign(body), SECRET))
    check("signature_missing_rejected", not gh.verify_signature(body, None, SECRET))
    check("signature_wrong_scheme_rejected", not gh.verify_signature(body, "sha1=abc", SECRET))

    # --- 2. event parsing ---------------------------------------------------
    pr_payload = {
        "action": "opened",
        "pull_request": {
            "title": "Add sub function", "body": "subtracts b from a",
            "head": {"sha": "h" * 40}, "base": {"sha": "b" * 40}, "number": 7,
        },
        "repository": {"full_name": "user/repo", "clone_url": "https://github.com/user/repo.git"},
    }
    parsed = gh.parse_pull_request_event(pr_payload)
    check("pr_event_parsed", parsed is not None and parsed["intent"] == "Add sub function\n\nsubtracts b from a"
          and parsed["pr_number"] == 7)
    check("closed_action_ignored", gh.parse_pull_request_event({**pr_payload, "action": "closed"}) is None)

    # --- 3. clone/fetch + forced checkout of the exact head ----------------
    origin, base_sha, head_sha = make_origin_repo()
    os.environ[gh.REPOS_DIR_ENV] = str(Path(tempfile.mkdtemp()) / "repos")
    dest = gh.prepare_repo(str(origin), "user/repo", head_sha)
    content = (dest / "calc.py").read_text(encoding="utf-8")
    check("clone_and_checkout_head", "def sub" in content, "working tree == PR head commit")
    dest2 = gh.prepare_repo(str(origin), "user/repo", base_sha)  # re-prep to a different sha
    check("reprep_moves_working_tree", "def sub" not in (dest2 / "calc.py").read_text(encoding="utf-8"),
          "second prep correctly checked out the base commit instead")

    # --- 4. conclusion mapping (the 3-way contract, on the PR) -------------
    check("conclusion_low_success", gh.check_conclusion("completed", "LOW") == "success")
    check("conclusion_high_failure", gh.check_conclusion("completed", "HIGH") == "failure")
    check("conclusion_unverified_failure", gh.check_conclusion("unverified", "UNVERIFIED") == "failure")
    check("conclusion_errored_neutral", gh.check_conclusion("errored", None) == "neutral",
          "checker broke != code risky")

    # --- 5. format_github ----------------------------------------------------
    from verdict.reporter import format_github

    record = {
        "run_id": "run_gh1", "status": "completed", "model": "groq/x",
        "scope": "commit range abc..def",
        "risk": {"level": "HIGH", "passed": 1, "failed": 1, "inconclusive": 0,
                 "coverage": 0.5, "reasons": ["FAILED sub_is_wrong (exit 1)"]},
        "results": [
            {"scenario_name": "add_ok", "status": "passed", "stdout": "sum ok", "duration_s": 1, "exit_code": 0},
            {"scenario_name": "sub_is_wrong", "status": "failed", "stdout": "expected 2 got 3", "duration_s": 1, "exit_code": 1},
        ],
    }
    md = format_github(record)
    check("markdown_has_verdict_and_table",
          "Verdict: HIGH RISK" in md and "| `sub_is_wrong` | ❌ FAILED |" in md and "commit range abc..def" in md)
    md_err = format_github({"run_id": "run_e", "status": "errored", "failed_stage": "config", "reason": "provider down"})
    check("markdown_errored_is_neutral_phrased", "checker problem" in md_err and "not evidence" in md_err)

    # --- 6. Checks API payload shape (urllib captured, nothing sent) -------
    captured = {}

    def fake_urlopen(req, timeout=30):
        captured["url"] = req.full_url
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        captured["auth"] = req.headers.get("Authorization")

        class R:
            def read(self):
                return b'{"id": 1}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        return R()

    os.environ[gh.GITHUB_TOKEN_ENV] = "ghs_test_token"
    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        gh.post_check_run("user/repo", "h" * 40, record, "completed")
    p = captured["payload"]
    check("check_run_payload_shape",
          captured["url"].endswith("/repos/user/repo/check-runs")
          and p["name"] == "verdict" and p["conclusion"] == "failure"
          and p["head_sha"] == "h" * 40 and "Verdict: HIGH" in p["output"]["summary"]
          and captured["auth"] == "Bearer ghs_test_token")

    # --- 7. webhook endpoint end-to-end (TestClient, real Postgres) --------
    os.environ["VERDICT_DATABASE_URL"] = DATABASE_URL
    os.environ[gh.WEBHOOK_SECRET_ENV] = SECRET
    from fastapi.testclient import TestClient

    from verdict.server import api as api_mod

    client = TestClient(api_mod.app)

    r = client.post("/webhooks/github", content=body, headers={"X-Hub-Signature-256": "sha256=bad", "X-GitHub-Event": "ping"})
    check("webhook_bad_signature_401", r.status_code == 401)

    ping = client.post("/webhooks/github", content=body, headers={"X-Hub-Signature-256": sign(body), "X-GitHub-Event": "ping"})
    check("webhook_ping_pong", ping.status_code == 200 and ping.json()["detail"] == "pong")

    push_body = json.dumps({"action": "opened"}).encode()
    r = client.post("/webhooks/github", content=push_body, headers={"X-Hub-Signature-256": sign(push_body), "X-GitHub-Event": "push"})
    check("webhook_non_pr_event_ignored", r.status_code == 200 and "ignored" in r.json()["detail"])

    event_body = json.dumps({
        **pr_payload,
        "repository": {"full_name": "user/repo", "clone_url": str(origin)},
        "pull_request": {**pr_payload["pull_request"], "head": {"sha": head_sha}, "base": {"sha": base_sha}},
    }).encode()
    with patch("verdict.server.queue.execute_run_task", MagicMock()) as task:
        r = client.post("/webhooks/github", content=event_body,
                        headers={"X-Hub-Signature-256": sign(event_body), "X-GitHub-Event": "pull_request"})
        ok = r.status_code == 200 and r.json()["ok"] and not r.json()["deduped"] and task.delay.called
        check("webhook_pr_creates_and_enqueues_job", ok, f"job {r.json().get('job_id')}, run {r.json().get('run_id')}")

        r2 = client.post("/webhooks/github", content=event_body,
                         headers={"X-Hub-Signature-256": sign(event_body), "X-GitHub-Event": "pull_request"})
        check("webhook_same_head_sha_deduped", r2.status_code == 200 and r2.json().get("deduped") is True)

    del os.environ[gh.WEBHOOK_SECRET_ENV]
    r = client.post("/webhooks/github", content=body, headers={"X-GitHub-Event": "ping"})
    check("webhook_disabled_without_secret", r.status_code == 503)

    passed = sum(1 for c in checks if c["ok"])
    result = {
        "suite": "Phase 4 - GitHub integration (local verification; live gate = action/ on a real PR)",
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "checks": checks, "passed": passed, "total": len(checks),
        "all_passed": passed == len(checks),
    }
    RESULTS_FILE.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"\n{passed}/{len(checks)} checks passed")
    return 0 if result["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
