"""
Phase 3 gate (Section 12): "5 concurrent runs complete correctly, no dropped
jobs, no double-runs; health reports all dependencies accurately."

Fully real end-to-end: a real API process (uvicorn), a real Celery worker
process, real Redis + Postgres + Docker sandboxes. The only mock is the LLM
itself - a local OpenAI-compatible HTTP server with canned responses, wired
in through the standard provider=custom config (so even the transport layer
under test is the real one).

Prereqs (the gate checks and reports if missing):
    docker run -d --name verdict-postgres -p 5433:5432 -e POSTGRES_USER=verdict \
        -e POSTGRES_PASSWORD=verdict -e POSTGRES_DB=verdict postgres:16-alpine
    docker run -d --name verdict-redis -p 6380:6379 redis:7-alpine

Writes phase3/gate_results.json.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DATABASE_URL = os.environ.get("VERDICT_DATABASE_URL", "postgresql://verdict:verdict@localhost:5433/verdict")
REDIS_URL = os.environ.get("VERDICT_REDIS_URL", "redis://localhost:6380/0")
API_PORT = 8411
LLM_PORT = 8990
RESULTS_FILE = Path(__file__).parent / "gate_results.json"

SCENARIOS_JSON = json.dumps({
    "scenarios": [
        {"name": "add_returns_sum", "description": "add(2,3) must return 5 - the calc add function sums its two arguments"},
        {"name": "add_handles_negatives", "description": "add(-1,1) must return 0 - the calc add function handles negative arguments"},
    ]
})
TEST_ADD = "from calc import add\nassert add(2, 3) == 5\nprint('sum ok')\n"
TEST_NEG = "from calc import add\nassert add(-1, 1) == 0\nprint('negatives ok')\n"


class MockLLM(BaseHTTPRequestHandler):
    def log_message(self, *a):  # keep gate output clean
        pass

    def do_GET(self):
        if self.path.endswith("/models"):
            self._json({"data": [{"id": "gate-model"}]})
        else:
            self.send_error(404)

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8")
        payload = json.loads(body)
        prompt = payload["messages"][0]["content"]
        if "Respond with ONLY valid JSON" in prompt:
            text = SCENARIOS_JSON
        elif "add_handles_negatives" in prompt:
            text = TEST_NEG
        else:
            text = TEST_ADD
        self._json({
            "choices": [{"message": {"content": text}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10},
        })

    def _json(self, obj):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def make_repo(idx: int) -> Path:
    d = Path(tempfile.mkdtemp()) / f"repo{idx}"
    d.mkdir(parents=True)
    kw = dict(cwd=d, capture_output=True, text=True)
    subprocess.run(["git", "init", "-q"], **kw)
    subprocess.run(["git", "config", "user.email", "t@t.t"], **kw)
    subprocess.run(["git", "config", "user.name", "t"], **kw)
    (d / "calc.py").write_text(f"# repo {idx}\ndef add(a, b):\n    return a + b\n", encoding="utf-8")
    (d / ".verdict").mkdir()
    (d / ".verdict" / "config.json").write_text(json.dumps({
        "provider": "custom", "model": "gate-model",
        "base_url": f"http://127.0.0.1:{LLM_PORT}", "api_key": "gate-key",
    }), encoding="utf-8")
    subprocess.run(["git", "add", "."], **kw)
    subprocess.run(["git", "commit", "-q", "-m", "add calculator module with an add function summing its arguments"], **kw)
    return d


def main() -> int:
    import httpx

    from verdict import store

    checks: list[dict] = []

    def check(name, ok, detail=""):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  - {detail}" if detail else ""))

    env = {**os.environ, "VERDICT_DATABASE_URL": DATABASE_URL, "VERDICT_REDIS_URL": REDIS_URL}
    store.init_schema(DATABASE_URL)
    with store.connect(DATABASE_URL) as conn:
        conn.execute("DELETE FROM jobs")  # a clean queue-state slate for the gate

    llm_server = ThreadingHTTPServer(("127.0.0.1", LLM_PORT), MockLLM)
    threading.Thread(target=llm_server.serve_forever, daemon=True).start()

    root = Path(__file__).resolve().parent.parent
    api = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "verdict.server.api:app", "--port", str(API_PORT), "--log-level", "error"],
        cwd=root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    worker = subprocess.Popen(
        [sys.executable, "-m", "verdict.cli", "worker", "--concurrency", "5"],
        cwd=root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{API_PORT}"

    try:
        for _ in range(40):
            try:
                httpx.get(f"{base}/health", timeout=2)
                break
            except httpx.HTTPError:
                time.sleep(0.5)
        else:
            check("api_started", False, "API never came up")
            return _finish(checks)

        # --- 1. health honest while everything is up ----------------------
        h = httpx.get(f"{base}/health", timeout=10)
        comps = h.json()["components"]
        check("health_all_up", h.status_code == 200 and h.json()["healthy"],
              ", ".join(f"{k}={'ok' if v['ok'] else 'DOWN'}" for k, v in comps.items()))

        m = httpx.get(f"{base}/metrics", timeout=10)
        check("metrics_prometheus_format", "verdict_health_status{" in m.text and "verdict_queue_depth" in m.text)

        # --- 2. five concurrent runs --------------------------------------
        repos = [make_repo(i) for i in range(5)]
        t0 = time.monotonic()

        def submit(repo: Path):
            return httpx.post(f"{base}/runs", json={"repo_path": str(repo)}, timeout=30).json()

        with ThreadPoolExecutor(max_workers=5) as pool:
            submissions = list(pool.map(submit, repos))
        check("five_submitted", all(s.get("status") == "queued" for s in submissions),
              f"job ids {[s.get('job_id') for s in submissions]}")

        deadline = time.monotonic() + 300
        final = {}
        while time.monotonic() < deadline and len(final) < 5:
            for s in submissions:
                jid = s["job_id"]
                if jid in final:
                    continue
                job = httpx.get(f"{base}/jobs/{jid}", timeout=10).json()
                if job["status"] in ("completed", "errored", "skipped", "unverified"):
                    final[jid] = job
            time.sleep(1)
        wall = round(time.monotonic() - t0, 1)

        check("five_completed_no_drops", len(final) == 5 and all(j["status"] == "completed" for j in final.values()),
              f"statuses={[j['status'] for j in final.values()]}, wall={wall}s")

        run_ids = [j["run_id"] for j in final.values()]
        check("five_distinct_run_ids", len(set(run_ids)) == 5, f"{sorted(run_ids)}")

        records = [store.load_run_record(DATABASE_URL, rid) for rid in run_ids]
        check("five_records_low_risk", all(r and (r.get("risk") or {}).get("level") == "LOW" for r in records),
              "each: 2 scenarios passed -> LOW")
        with store.connect(DATABASE_URL) as conn:
            job_count = conn.execute("SELECT count(*) FROM jobs").fetchone()[0]
            result_rows = conn.execute(
                "SELECT count(*) FROM results WHERE run_id = ANY(%s)", (run_ids,)
            ).fetchone()[0]
        check("no_double_runs", job_count == 5 and result_rows == 10,
              f"5 jobs, {result_rows} result rows (2 scenarios x 5 runs, no duplicates)")

        # --- 3. dedupe: same repo+commit+model -> no second run -----------
        again = submit(repos[0])
        check("dedupe_same_commit", again.get("deduped") is True and again.get("job_id") == submissions[0]["job_id"],
              f"resubmit returned existing job {again.get('job_id')}")
        forced = httpx.post(f"{base}/runs", json={"repo_path": str(repos[0]), "force": True}, timeout=30).json()
        check("force_bypasses_dedupe", forced.get("deduped") is False, f"forced new job {forced.get('job_id')}")

        # --- 4. health honesty: stop redis, watch it tell the truth -------
        subprocess.run(["docker", "stop", "verdict-redis"], capture_output=True)
        time.sleep(1)
        h2 = httpx.get(f"{base}/health", timeout=15)
        check("health_reports_redis_down", h2.status_code == 503 and not h2.json()["components"]["redis"]["ok"],
              h2.json()["components"]["redis"]["detail"][:70])
        refused = httpx.post(f"{base}/runs", json={"repo_path": str(repos[1]), "force": True}, timeout=15)
        check("refuses_work_while_down", refused.status_code == 503,
              "new work refused outright, not queued into an inconsistent state")
        subprocess.run(["docker", "start", "verdict-redis"], capture_output=True)
        time.sleep(2)
        h3 = httpx.get(f"{base}/health", timeout=15)
        check("health_recovers", h3.status_code == 200 and h3.json()["healthy"])

        return _finish(checks, extra={"five_run_wall_seconds": wall})
    finally:
        api.terminate()
        worker.terminate()
        llm_server.shutdown()
        subprocess.run(["docker", "start", "verdict-redis"], capture_output=True)


def _finish(checks, extra=None) -> int:
    passed = sum(1 for c in checks if c["ok"])
    result = {
        "gate": "Phase 3 - 5 concurrent runs, no drops/dupes; honest health",
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "checks": checks,
        "passed": passed,
        "total": len(checks),
        "gate_met": passed == len(checks),
        **(extra or {}),
    }
    RESULTS_FILE.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"\n{passed}/{len(checks)} checks passed -> {'GATE MET' if result['gate_met'] else 'GATE NOT MET'}")
    print(f"evidence: {RESULTS_FILE}")
    return 0 if result["gate_met"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
