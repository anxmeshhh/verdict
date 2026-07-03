"""
Module 18 - Health & Resilience (Phase 3).

Liveness checks for every dependency, shared by `verdict health` (CLI) and
GET /health (API). Governing principle from the direction doc: degrade
honestly, never fail silently - if health is uncertain, the system must not
produce a verdict that looks confident.

Only what's configured gets checked: a plain CLI setup with no Redis is not
"unhealthy", it just has fewer components.
"""
import os
import shutil
from dataclasses import dataclass

REDIS_URL_ENV = "VERDICT_REDIS_URL"
DEFAULT_REDIS_URL = "redis://localhost:6379/0"

# Two thresholds, per the direction doc's own example ("Disk WARN 82% used"
# with the system still operational): crossing WARN is a loud heads-up while
# everything keeps working; only CRITICAL marks the component unhealthy and
# blocks new runs before disk-full corrupts state mid-run.
DISK_WARN_FRACTION = 0.85
DISK_CRITICAL_FRACTION = 0.95


@dataclass
class ComponentHealth:
    component: str
    ok: bool
    detail: str
    # 1.0 healthy, 0.0 down; fractional for capacity-style components
    value: float = 1.0


def resolve_redis_url() -> str:
    return os.environ.get(REDIS_URL_ENV, "").strip() or DEFAULT_REDIS_URL


def check_redis(redis_url: str | None = None, timeout: float = 3.0) -> ComponentHealth:
    url = redis_url or resolve_redis_url()
    try:
        import redis as redis_lib
    except ImportError:
        return ComponentHealth("redis", False, "redis client not installed - pip install 'verdict[server]'", 0.0)
    try:
        client = redis_lib.Redis.from_url(url, socket_connect_timeout=timeout, socket_timeout=timeout)
        client.ping()
        return ComponentHealth("redis", True, "connected", 1.0)
    except Exception as e:
        return ComponentHealth("redis", False, str(e), 0.0)


def check_disk(
    path: str = ".",
    warn_fraction: float = DISK_WARN_FRACTION,
    critical_fraction: float = DISK_CRITICAL_FRACTION,
) -> ComponentHealth:
    usage = shutil.disk_usage(path)
    used_fraction = 1 - (usage.free / usage.total)
    detail = f"{used_fraction:.0%} used ({usage.free // (1024**3)} GB free)"
    if used_fraction >= critical_fraction:
        detail = f"CRITICAL - {detail} - blocking new runs before disk-full corrupts state"
    elif used_fraction >= warn_fraction:
        detail = f"WARN - {detail}"
    return ComponentHealth("disk", used_fraction < critical_fraction, detail, round(1 - used_fraction, 3))


def check_postgres(database_url: str) -> ComponentHealth:
    from verdict import store

    ok, detail = store.check(database_url)
    return ComponentHealth("postgres", ok, detail, 1.0 if ok else 0.0)


def check_queue(database_url: str) -> ComponentHealth:
    """Queue depth from the jobs table - visible backpressure, never a
    silent pile-up."""
    from verdict import store

    try:
        counts = store.queue_depth(database_url)
    except store.StoreError as e:
        return ComponentHealth("queue", False, str(e), 0.0)
    pending = counts.get("pending_total", 0)
    queued = counts.get("queued", 0)
    running = sum(n for s, n in counts.items() if s.startswith("running"))
    waiting = counts.get("waiting_on_llm", 0)
    detail = f"{queued} queued, {running} running"
    if waiting:
        detail += f", {waiting} waiting_on_llm"
    return ComponentHealth("queue", True, detail, float(pending))
