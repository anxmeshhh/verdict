"""
Phase 5 gate (Section 12): "A stranger clones the repo and gets a working
run, unaided, in under 10 minutes."

Simulates the stranger literally: `git clone` of this repo into a temp dir
(so only committed files count - anything untracked that the stack needs
would fail here), write .env the way setup.sh would, `docker compose up -d
--build`, wait healthy, drop a sample repo under data/repos, POST a run
through the real API, and poll it to a LOW verdict. The wall clock for all
of it must be under 600 seconds.

The LLM is the local mock server (provider=custom via host.docker.internal)
- same transport, same config path a stranger uses with a real key; noted
honestly in the evidence. Everything else is the real stack: built image,
compose networking, DooD sandbox path mapping, API auth.

Writes phase5/gate_results.json.
"""
import json
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "phase3"))

from gate import MockLLM  # noqa: E402  (phase3's canned-response server)

LLM_PORT = 8990
API = "http://localhost:8400"
RESULTS_FILE = Path(__file__).parent / "gate_results.json"

checks: list[dict] = []


def check(name, ok, detail=""):
    checks.append({"check": name, "ok": bool(ok), "detail": detail})
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  - {detail}" if detail else ""))


def sh(cmd, cwd=None, timeout=600):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def main() -> int:
    import httpx

    source = Path(__file__).resolve().parent.parent
    workdir = Path(tempfile.mkdtemp())
    clone = workdir / "verdict"
    api_key = secrets.token_hex(16)
    t0 = time.monotonic()

    # --- the stranger's clone (committed files only) -----------------------
    r = sh(["git", "clone", "-q", str(source), str(clone)])
    check("clean_clone", r.returncode == 0, r.stderr.strip()[:100])

    # --- .env exactly as setup.sh would write it ----------------------------
    data_dir = clone / "data"
    (data_dir / "repos").mkdir(parents=True)
    (data_dir / "tmp").mkdir(parents=True)
    env_text = (clone / ".env.example").read_text(encoding="utf-8")
    env_text = env_text.replace("HOST_DATA_DIR=", f"HOST_DATA_DIR={data_dir}")
    env_text = env_text.replace("VERDICT_SERVER_API_KEY=", f"VERDICT_SERVER_API_KEY={api_key}")
    (clone / ".env").write_text(env_text, encoding="utf-8")
    check("env_written", True, "HOST_DATA_DIR + generated API key, like setup.sh")

    # --- mock LLM reachable from containers via host.docker.internal --------
    llm_server = ThreadingHTTPServer(("0.0.0.0", LLM_PORT), MockLLM)
    threading.Thread(target=llm_server.serve_forever, daemon=True).start()

    try:
        # --- build + start the real stack -----------------------------------
        up = sh(["docker", "compose", "up", "-d", "--build"], cwd=clone, timeout=540)
        check("compose_up_builds", up.returncode == 0, up.stderr.strip().splitlines()[-1][:120] if up.returncode else f"{round(time.monotonic() - t0)}s in")
        if up.returncode != 0:
            return _finish(t0)

        for _ in range(90):
            try:
                if httpx.get(f"{API}/health", timeout=3).status_code in (200, 503):
                    break
            except httpx.HTTPError:
                pass
            time.sleep(2)
        h = httpx.get(f"{API}/health", timeout=10)
        check("stack_healthy", h.status_code == 200 and h.json()["healthy"],
              ", ".join(f"{k}={'ok' if v['ok'] else 'DOWN'}" for k, v in h.json()["components"].items()))

        # --- auth actually enforced -----------------------------------------
        no_key = httpx.get(f"{API}/runs", timeout=10)
        check("api_key_enforced", no_key.status_code == 401)

        # --- sample repo under data/repos, then a real run through the API --
        sample = data_dir / "repos" / "sample"
        sample.mkdir(parents=True)
        kw = dict(cwd=sample, capture_output=True, text=True)
        subprocess.run(["git", "init", "-q"], **kw)
        subprocess.run(["git", "config", "user.email", "s@s.s"], **kw)
        subprocess.run(["git", "config", "user.name", "s"], **kw)
        (sample / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        (sample / ".verdict").mkdir()
        (sample / ".verdict" / "config.json").write_text(json.dumps({
            "provider": "custom", "model": "gate-model",
            "base_url": f"http://host.docker.internal:{LLM_PORT}", "api_key": "gate-key",
        }), encoding="utf-8")
        subprocess.run(["git", "add", "."], **kw)
        subprocess.run(["git", "commit", "-q", "-m", "add calculator module with an add function summing its arguments"], **kw)

        headers = {"X-API-Key": api_key}
        sub = httpx.post(f"{API}/runs", json={"repo_path": "/data/repos/sample"}, headers=headers, timeout=30)
        check("run_submitted", sub.status_code == 200 and sub.json()["status"] == "queued",
              str(sub.json())[:100])
        job_id = sub.json()["job_id"]

        final = None
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            job = httpx.get(f"{API}/jobs/{job_id}", headers=headers, timeout=10).json()
            if job["status"] in ("completed", "errored", "skipped", "unverified") or job["status"].startswith("failed"):
                final = job
                break
            time.sleep(2)
        check("run_completed", final is not None and final["status"] == "completed",
              f"job status: {final['status'] if final else 'timed out'}")

        if final and final["status"] == "completed":
            run = httpx.get(f"{API}/runs/{final['run_id']}", headers=headers, timeout=10).json()
            check("verdict_low_through_full_stack", (run.get("risk") or {}).get("level") == "LOW",
                  f"{run.get('risk', {}).get('passed')} passed - via compose api -> redis -> worker -> DooD sandbox")

        elapsed = round(time.monotonic() - t0, 1)
        check("under_ten_minutes", elapsed < 600, f"{elapsed}s total (clone -> build -> up -> verified run)")
        return _finish(t0, elapsed)
    finally:
        sh(["docker", "compose", "down", "-v"], cwd=clone, timeout=180)
        llm_server.shutdown()


def _finish(t0, elapsed=None) -> int:
    passed = sum(1 for c in checks if c["ok"])
    result = {
        "gate": "Phase 5 - a stranger clones and gets a working run in <10 minutes",
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "method": "literal git clone to a temp dir + .env as setup.sh writes it + docker compose up -d --build "
                  "+ POST /runs through the real API. LLM = local mock via provider=custom/host.docker.internal "
                  "(same transport and config path as a real key; the model itself is not under test here).",
        "checks": checks,
        "passed": passed,
        "total": len(checks),
        "gate_met": passed == len(checks),
        "elapsed_seconds": elapsed,
    }
    RESULTS_FILE.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"\n{passed}/{len(checks)} checks passed -> {'GATE MET' if result['gate_met'] else 'GATE NOT MET'}"
          + (f" in {elapsed}s" if elapsed else ""))
    return 0 if result["gate_met"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
