"""
Module 10 - Queue & Worker Pool (Phase 3).

Celery on Redis. Each task drives the exact same execute_pipeline() the CLI
uses - the worker is just another frontend, with a DB-status events adapter
instead of a Rich one.

Reliability rules (Section 11/12):
- acks_late + an idempotency guard on job status: a redelivered task for an
  already-completed job is a no-op, never a double-run. The UNIQUE
  dedupe_key in Postgres already prevents duplicate submissions upstream.
- LLM down -> the job stays queued as waiting_on_llm and the task retries
  with backoff. Never dropped, never faked.
- Sandbox concurrency is capped by worker concurrency (the doc's
  MAX_CONCURRENT_SANDBOX_RUNS): scenarios within one run stay sequential,
  so concurrent containers == concurrent jobs.
"""
from pathlib import Path

from celery import Celery

from verdict import llm, store
from verdict.config import load_config
from verdict.health import resolve_redis_url
from verdict.pipeline import PipelineEvents, PipelineParams, execute_pipeline

WORKER_CONCURRENCY_ENV = "VERDICT_WORKER_CONCURRENCY"
DEFAULT_WORKER_CONCURRENCY = 2  # the doc's MAX_CONCURRENT_SANDBOX_RUNS example

LLM_RETRY_COUNTDOWN = 30  # seconds between waiting_on_llm retries
LLM_MAX_RETRIES = 20      # ~10 minutes of provider outage before giving up

celery_app = Celery("verdict", broker=resolve_redis_url(), backend=resolve_redis_url())
celery_app.conf.update(
    task_acks_late=True,
    worker_prefetch_multiplier=1,  # a slow sandbox run must not hold hostages
    task_reject_on_worker_lost=True,
    broker_connection_retry_on_startup=True,
)


def database_url() -> str:
    url = store.resolve_database_url()
    if not url:
        raise store.StoreError(
            "server mode requires the data layer - set VERDICT_DATABASE_URL"
        )
    return url


class _DbEvents(PipelineEvents):
    """Stage transitions land in jobs.status so GET /jobs/<id> answers
    'where is my run right now' without the worker's stdout."""

    def __init__(self, url: str, job_id: int):
        self.url, self.job_id = url, job_id

    def _set(self, status: str) -> None:
        try:
            store.update_job(self.url, self.job_id, status)
        except store.StoreError:
            pass  # status display is best-effort; the run itself continues

    def stage_ok(self, name: str, detail: str = "") -> None:
        self._set(f"running:{name}")

    def stage_fail(self, name: str, detail: str) -> None:
        self._set(f"failed:{name}")


@celery_app.task(bind=True, name="verdict.execute_run", max_retries=LLM_MAX_RETRIES)
def execute_run_task(self, job_id: int):
    url = database_url()
    job = store.get_job(url, job_id)
    if job is None:
        return {"job_id": job_id, "status": "missing"}
    # Idempotency guard: acks_late can redeliver after a worker crash - a
    # finished job must never run twice.
    if job["status"].startswith(("completed", "errored", "skipped", "unverified")):
        return {"job_id": job_id, "status": job["status"], "note": "already finished - redelivery ignored"}

    params_dict = dict(job["params"])
    repo = Path(params_dict.pop("repo_path"))
    github_ctx = params_dict.pop("github", None)
    run_id = job["run_id"]
    config = load_config(repo)

    # Health gate (Section 11): LLM down -> stay queued as waiting_on_llm,
    # retry with backoff. Never dropped, never a fake verdict.
    status = llm.check(config)
    if not status.reachable:
        store.update_job(url, job_id, "waiting_on_llm")
        raise self.retry(countdown=LLM_RETRY_COUNTDOWN)

    if "paths" in params_dict:
        params_dict["paths"] = params_dict["paths"] or None
    sf = params_dict.get("scenarios_file")
    params_dict["scenarios_file"] = Path(sf) if sf else None
    params = PipelineParams(**params_dict)

    store.update_job(url, job_id, "running")
    outcome = execute_pipeline(params, config, repo, events=_DbEvents(url, job_id), run_id=run_id)

    # The pipeline dual-writes only when the TARGET REPO's config has a
    # database_url - in server mode the record must land in the server's DB
    # regardless, so write it explicitly (idempotent upsert).
    store.save_run_record(url, outcome.record)
    store.update_job(url, job_id, outcome.status)

    check_posted = None
    if github_ctx:
        # Module 11: the verdict lands on the PR, where reviewers already look.
        from verdict.server import github as gh

        try:
            gh.post_check_run(github_ctx["repo_full_name"], github_ctx["head_sha"], outcome.record, outcome.status)
            check_posted = True
        except gh.GitHubError as e:
            # The run itself succeeded and is fully recorded - a failed check
            # post is loud in the job status, never silently swallowed.
            check_posted = False
            store.update_job(url, job_id, f"{outcome.status}:check_post_failed")
            print(f"verdict worker: check post failed for job {job_id}: {e}")

    return {"job_id": job_id, "run_id": run_id, "status": outcome.status,
            "risk": outcome.risk_level, "check_posted": check_posted}
