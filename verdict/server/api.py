"""
API Gateway (Phase 3) - FastAPI.

Decouples ingestion from processing: POST /runs validates + dedupes +
enqueues, workers execute, everything readable back out of Postgres.

Honesty rules (Section 11):
- Postgres or Redis down -> refuse new work outright (503), never operate
  in an inconsistent state. Reads may still work if only Redis is down.
- Idempotency: same (repo, base, head SHA, model, prompt-contract version)
  never runs twice - enforced by a UNIQUE constraint, surfaced as
  {"deduped": true} instead of a second run.
- GET /health and /metrics never require auth: an unhealthy system must be
  observable while it's unhealthy.
"""
import hashlib
import json
import os
import subprocess
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

from verdict import health as health_mod
from verdict import store
from verdict.config import load_config
from verdict.generator import CACHE_VERSION
from verdict.reporter import new_run_id
from verdict.sandbox import check_docker

API_KEY_ENV = "VERDICT_SERVER_API_KEY"

app = FastAPI(
    title="Verdict",
    description="Proof, not vibes - pre-deployment verification for AI-written code.",
    version="0.1.0",
)


@app.on_event("startup")
def _init_schema_on_startup() -> None:
    """Best-effort schema init so GET /runs works before the first submit.
    Postgres not up yet (compose race) is fine - every write path re-inits."""
    url = store.resolve_database_url()
    if url:
        try:
            store.init_schema(url)
        except store.StoreError:
            pass


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    expected = os.environ.get(API_KEY_ENV, "").strip()
    if not expected:
        return  # local/dev mode: no key configured -> open (compose sets one)
    if x_api_key != expected:
        raise HTTPException(status_code=401, detail="missing or wrong X-API-Key")


def database_url() -> str:
    url = store.resolve_database_url()
    if not url:
        raise HTTPException(status_code=503, detail="no VERDICT_DATABASE_URL configured - server mode needs the data layer")
    return url


class RunRequest(BaseModel):
    repo_path: str = Field(description="Path to the git repo on the server/worker filesystem")
    ref: str | None = None
    base: str | None = None
    intent: str | None = None
    paths: list[str] | None = None
    max_scenarios: int = 8
    timeout: int = 300
    force_regenerate: bool = False
    force: bool = Field(default=False, description="Re-run even if this exact commit+model already has a job")


class OverrideRequest(BaseModel):
    reason: str
    actor: str = "api"


def _resolve_head_sha(repo: Path, ref: str | None) -> str:
    proc = subprocess.run(
        ["git", "rev-parse", ref or "HEAD"],
        cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise HTTPException(status_code=400, detail=f"cannot resolve ref '{ref or 'HEAD'}': {proc.stderr.strip()}")
    return proc.stdout.strip()


@app.post("/runs", dependencies=[Depends(require_api_key)])
def submit_run(req: RunRequest):
    url = database_url()

    # Refuse new work when the backbone is down - degrade honestly.
    db_ok, db_detail = store.check(url)
    if not db_ok:
        raise HTTPException(status_code=503, detail=f"postgres down - refusing new work: {db_detail}")
    redis_health = health_mod.check_redis()
    if not redis_health.ok:
        raise HTTPException(status_code=503, detail=f"redis down - refusing new work: {redis_health.detail}")

    repo = Path(req.repo_path)
    if not (repo / ".git").exists():
        raise HTTPException(status_code=400, detail=f"{req.repo_path} is not a git repository on this server")
    head_sha = _resolve_head_sha(repo, req.ref)
    config = load_config(repo)

    key_material = f"{repo.resolve()}|{req.base or ''}|{req.ref or 'HEAD'}|{head_sha}|{config.model}|v{CACHE_VERSION}"
    if req.force:
        key_material += f"|force-{new_run_id()}"  # a forced rerun is deliberately never deduped
    dedupe_key = hashlib.sha256(key_material.encode("utf-8")).hexdigest()[:32]

    params = {
        "repo_path": str(repo.resolve()),
        "ref": req.ref, "base": req.base, "intent": req.intent,
        "paths": req.paths, "max_scenarios": req.max_scenarios,
        "timeout": req.timeout, "force_regenerate": req.force_regenerate,
    }
    run_id = new_run_id()
    store.init_schema(url)
    job, created = store.create_job(url, dedupe_key, params, run_id)
    if not created:
        return {"job_id": job["job_id"], "run_id": job["run_id"], "status": job["status"],
                "deduped": True, "detail": "an identical submission already exists (same repo, commit, model)"}

    from verdict.server.queue import execute_run_task

    try:
        execute_run_task.delay(job["job_id"])
    except Exception as e:
        # A job row with no task behind it would dedupe-block this commit
        # forever - roll it back so a later submit can succeed.
        store.delete_job(url, job["job_id"])
        raise HTTPException(status_code=503, detail=f"could not enqueue (broker down?): {e}")
    return {"job_id": job["job_id"], "run_id": run_id, "status": "queued", "deduped": False}


@app.get("/runs", dependencies=[Depends(require_api_key)])
def list_runs(limit: int = 25):
    return {"runs": [
        {k: r.get(k) for k in ("run_id", "created_at", "status", "model", "scope")}
        | {"risk": (r.get("risk") or {}).get("level"),
           "intent": (r.get("intent") or "").splitlines()[0][:80] if r.get("intent") else None}
        for r in store.list_run_records(database_url(), limit=limit)
    ]}


@app.get("/runs/{run_id}", dependencies=[Depends(require_api_key)])
def get_run(run_id: str):
    record = store.load_run_record(database_url(), run_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"no run named {run_id}")
    record.pop("diff", None)  # same slim contract as `verdict run --json`
    record.pop("generation_prompt", None)
    record.pop("generation_raw_response", None)
    for r in record.get("results", []):
        r.pop("test_code", None)
    overrides = store.get_overrides(database_url(), run_id)
    if overrides:
        record["overrides"] = overrides
    return record


@app.get("/jobs/{job_id}", dependencies=[Depends(require_api_key)])
def get_job(job_id: int):
    job = store.get_job(database_url(), job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"no job {job_id}")
    return job


@app.post("/runs/{run_id}/override", dependencies=[Depends(require_api_key)])
def override_run(run_id: str, req: OverrideRequest):
    if not req.reason.strip():
        raise HTTPException(status_code=400, detail="an override requires a real reason - it is the audit trail")
    try:
        entry = store.add_override(database_url(), run_id, req.reason.strip(), req.actor)
    except store.StoreError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"overridden": True, **entry, "rate": store.override_rate(database_url())}


def _collect_health() -> tuple[list, bool]:
    components: list[health_mod.ComponentHealth] = []
    url = store.resolve_database_url()
    if url:
        components.append(health_mod.check_postgres(url))
        components.append(health_mod.check_queue(url))
    else:
        components.append(health_mod.ComponentHealth("postgres", False, "no VERDICT_DATABASE_URL configured", 0.0))
    components.append(health_mod.check_redis())
    components.append(health_mod.ComponentHealth(
        "docker", check_docker(), "daemon reachable" if check_docker() else "daemon not reachable", 1.0
    ))
    components.append(health_mod.check_disk())
    # queue depth is informational; every other component must be ok
    all_ok = all(c.ok for c in components if c.component != "queue")
    return components, all_ok


@app.get("/health")
def get_health(response: Response):
    components, all_ok = _collect_health()
    response.status_code = 200 if all_ok else 503
    return {
        "healthy": all_ok,
        "components": {c.component: {"ok": c.ok, "detail": c.detail} for c in components},
    }


@app.get("/metrics")
def metrics():
    """Prometheus text format: verdict_health_status{component=...} per the
    direction doc's Section 11 observability tie-in."""
    lines = [
        "# HELP verdict_health_status 1 = healthy, 0 = down (fractional = capacity)",
        "# TYPE verdict_health_status gauge",
    ]
    components, _ = _collect_health()
    depth = 0.0
    for c in components:
        if c.component == "queue":
            depth = c.value
            lines.append(f'verdict_health_status{{component="queue"}} {1 if c.ok else 0}')
        else:
            lines.append(f'verdict_health_status{{component="{c.component}"}} {c.value if c.ok else 0}')
    lines += [
        "# HELP verdict_queue_depth jobs queued or running",
        "# TYPE verdict_queue_depth gauge",
        f"verdict_queue_depth {depth}",
    ]
    return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")


@app.post("/webhooks/github")
async def github_webhook(request: Request):
    """PR opened/updated -> verify signature -> clone/fetch -> enqueue.

    The check run is posted by the worker when the run finishes; this
    endpoint only validates and queues (Stage 1 of the doc's workflow:
    'webhook fires, job queued, deduped on commit SHA')."""
    from verdict.server import github as gh

    secret = os.environ.get(gh.WEBHOOK_SECRET_ENV, "").strip()
    if not secret:
        raise HTTPException(status_code=503, detail=f"webhooks disabled - set {gh.WEBHOOK_SECRET_ENV}")
    body = await request.body()
    if not gh.verify_signature(body, request.headers.get("X-Hub-Signature-256"), secret):
        raise HTTPException(status_code=401, detail="bad or missing X-Hub-Signature-256")

    event = request.headers.get("X-GitHub-Event", "")
    if event == "ping":
        return {"ok": True, "detail": "pong"}
    if event != "pull_request":
        return {"ok": True, "detail": f"event '{event}' ignored - only pull_request triggers verification"}

    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="payload is not JSON")
    pr = gh.parse_pull_request_event(payload)
    if pr is None:
        return {"ok": True, "detail": f"action '{payload.get('action')}' ignored"}

    url = database_url()
    db_ok, db_detail = store.check(url)
    if not db_ok:
        raise HTTPException(status_code=503, detail=f"postgres down - refusing new work: {db_detail}")

    try:
        repo = gh.prepare_repo(pr["clone_url"], pr["repo_full_name"], pr["head_sha"])
    except gh.GitHubError as e:
        raise HTTPException(status_code=502, detail=str(e))

    config = load_config(repo)
    dedupe_key = hashlib.sha256(
        f"gh|{pr['repo_full_name']}|{pr['base_sha']}|{pr['head_sha']}|{config.model}|v{CACHE_VERSION}".encode()
    ).hexdigest()[:32]
    params = {
        "repo_path": str(repo.resolve()),
        "base": pr["base_sha"], "ref": pr["head_sha"], "intent": pr["intent"],
        "paths": None, "max_scenarios": 8, "timeout": 300, "force_regenerate": False,
        "github": {"repo_full_name": pr["repo_full_name"], "head_sha": pr["head_sha"], "pr_number": pr["pr_number"]},
    }
    run_id = new_run_id()
    store.init_schema(url)
    job, created = store.create_job(url, dedupe_key, params, run_id)
    if not created:
        return {"ok": True, "deduped": True, "job_id": job["job_id"], "run_id": job["run_id"],
                "detail": "this commit is already queued/verified for this model"}

    from verdict.server.queue import execute_run_task

    try:
        execute_run_task.delay(job["job_id"])
    except Exception as e:
        store.delete_job(url, job["job_id"])
        raise HTTPException(status_code=503, detail=f"could not enqueue (broker down?): {e}")
    return {"ok": True, "deduped": False, "job_id": job["job_id"], "run_id": run_id,
            "pr": pr["pr_number"], "head_sha": pr["head_sha"]}


@app.get("/", dependencies=[Depends(require_api_key)])
def index():
    """Minimal read-only run history page - the CLI is still the only thing
    that acts (doc Section 8 design rule); this is a view, not a dashboard."""
    from fastapi.responses import HTMLResponse

    try:
        records = store.list_run_records(database_url(), limit=30)
    except store.StoreError as e:
        return HTMLResponse(f"<pre>data layer unreachable: {e}</pre>", status_code=503)
    rows = "".join(
        f"<tr><td>{r.get('run_id')}</td><td>{(r.get('created_at') or '')[:16]}</td>"
        f"<td class='{(r.get('risk') or {}).get('level', '')}'>{(r.get('risk') or {}).get('level', r.get('status'))}</td>"
        f"<td>{(r.get('scope') or '')}</td>"
        f"<td>{((r.get('intent') or '').splitlines() or [''])[0][:70]}</td></tr>"
        for r in records
    )
    return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8"><title>Verdict - runs</title>
<style>body{{font:14px ui-monospace,monospace;background:#0b0f14;color:#e5e7eb;padding:2rem}}
table{{border-collapse:collapse;width:100%}}td,th{{padding:.4rem .8rem;border-bottom:1px solid #1f2937;text-align:left}}
.LOW{{color:#22c55e}}.MEDIUM{{color:#eab308}}.HIGH{{color:#ef4444}}.UNVERIFIED{{color:#9ca3af}}
h1{{color:#22d3ee;letter-spacing:.3em;font-size:1rem}}</style></head><body>
<h1>VERDICT</h1><p style="color:#6b7280">read-only run history - the CLI is the only thing that acts</p>
<table><tr><th>run</th><th>when</th><th>verdict</th><th>checked</th><th>intent</th></tr>{rows}</table>
</body></html>""")
