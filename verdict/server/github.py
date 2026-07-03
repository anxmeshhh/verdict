"""
Module 11 - GitHub Integration (Phase 4), server side.

Webhook in -> job queued -> worker verifies -> check run + comment posted.

Auth model (deliberately simple): a token in VERDICT_GITHUB_TOKEN - either a
PAT or a GitHub App installation token; the code never needs to know which.
Webhook authenticity is HMAC-verified against VERDICT_GITHUB_WEBHOOK_SECRET,
timing-safe, and unsigned payloads are rejected outright when a secret is
configured - a forged webhook must never be able to trigger clones or runs.
"""
import hashlib
import hmac
import json
import os
import subprocess
import urllib.request
from pathlib import Path

WEBHOOK_SECRET_ENV = "VERDICT_GITHUB_WEBHOOK_SECRET"
GITHUB_TOKEN_ENV = "VERDICT_GITHUB_TOKEN"
REPOS_DIR_ENV = "VERDICT_REPOS_DIR"
DEFAULT_REPOS_DIR = "data/repos"
GITHUB_API = "https://api.github.com"

# Server-level provider defaults written into webhook clones (a fresh clone
# has no .verdict/config.json of its own).
PROVIDER_ENVS = ("VERDICT_PROVIDER", "VERDICT_MODEL", "VERDICT_BASE_URL", "VERDICT_OLLAMA_URL")

HANDLED_ACTIONS = ("opened", "synchronize", "reopened")


class GitHubError(Exception):
    pass


def verify_signature(body: bytes, signature_header: str | None, secret: str) -> bool:
    """X-Hub-Signature-256 check, timing-safe. No secret configured -> the
    caller decides (the API refuses webhooks entirely in that case)."""
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature_header.removeprefix("sha256="), expected)


def parse_pull_request_event(payload: dict) -> dict | None:
    """Extract what the pipeline needs from a pull_request event, or None if
    this event isn't one we act on. Intent = PR title + body - exactly the
    'stated intent' the whole product verifies against."""
    action = payload.get("action")
    pr = payload.get("pull_request")
    if not pr or action not in HANDLED_ACTIONS:
        return None
    title = (pr.get("title") or "").strip()
    body = (pr.get("body") or "").strip()
    return {
        "repo_full_name": payload["repository"]["full_name"],
        "clone_url": payload["repository"]["clone_url"],
        "head_sha": pr["head"]["sha"],
        "base_sha": pr["base"]["sha"],
        "pr_number": pr["number"],
        "intent": f"{title}\n\n{body}".strip(),
    }


def repos_dir() -> Path:
    return Path(os.environ.get(REPOS_DIR_ENV, "").strip() or DEFAULT_REPOS_DIR)


def prepare_repo(clone_url: str, full_name: str, head_sha: str) -> Path:
    """Clone or fetch, then hard-checkout the PR head. The sandbox mounts
    the working tree, so the working tree MUST be the exact commit under
    verification - a stale checkout would verify the wrong code."""
    token = os.environ.get(GITHUB_TOKEN_ENV, "").strip()
    if token and clone_url.startswith("https://"):
        clone_url = clone_url.replace("https://", f"https://x-access-token:{token}@", 1)

    dest = repos_dir() / full_name.replace("/", "__")
    kw = dict(capture_output=True, text=True, encoding="utf-8", errors="replace")
    if (dest / ".git").exists():
        fetch = subprocess.run(["git", "fetch", clone_url, head_sha], cwd=dest, **kw)
        if fetch.returncode != 0:
            raise GitHubError(f"fetch failed for {full_name}: {fetch.stderr.strip()[:200]}")
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        clone = subprocess.run(["git", "clone", clone_url, str(dest)], **kw)
        if clone.returncode != 0:
            raise GitHubError(f"clone failed for {full_name}: {clone.stderr.strip()[:200]}")
        fetch = subprocess.run(["git", "fetch", "origin", head_sha], cwd=dest, **kw)
        if fetch.returncode != 0:
            raise GitHubError(f"fetch of {head_sha[:7]} failed: {fetch.stderr.strip()[:200]}")
    checkout = subprocess.run(["git", "checkout", "--force", head_sha], cwd=dest, **kw)
    if checkout.returncode != 0:
        raise GitHubError(f"checkout of {head_sha[:7]} failed: {checkout.stderr.strip()[:200]}")

    _write_provider_config(dest)
    return dest


def _write_provider_config(repo: Path) -> None:
    """A fresh clone has no provider config - seed it from server env so the
    operator configures the LLM once, not per-repo."""
    overrides = {
        "provider": os.environ.get("VERDICT_PROVIDER", "").strip(),
        "model": os.environ.get("VERDICT_MODEL", "").strip(),
        "base_url": os.environ.get("VERDICT_BASE_URL", "").strip(),
        "ollama_url": os.environ.get("VERDICT_OLLAMA_URL", "").strip(),
    }
    overrides = {k: v for k, v in overrides.items() if v}
    if not overrides:
        return
    cfg_path = repo / ".verdict" / "config.json"
    existing = {}
    if cfg_path.exists():
        try:
            existing = json.loads(cfg_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = {}
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps({**existing, **overrides}, indent=2), encoding="utf-8")


def _github_request(method: str, url: str, token: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "verdict-server/0.1.0",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        raise GitHubError(f"GitHub API {method} {url} -> HTTP {e.code}: {detail}") from e
    except (urllib.error.URLError, OSError) as e:
        raise GitHubError(f"GitHub API unreachable: {e}") from e


def check_conclusion(outcome_status: str, risk_level: str | None) -> str:
    """The 3-way exit-code contract, translated to Checks API conclusions:
    LOW -> success; risky verdict -> failure; verdict-couldn't-verify ->
    neutral (alert the checker's owner, don't blame the code)."""
    if outcome_status in ("errored", "skipped"):
        return "neutral"
    if outcome_status == "completed" and risk_level == "LOW":
        return "success"
    return "failure"


def post_check_run(repo_full_name: str, head_sha: str, record: dict, outcome_status: str) -> dict:
    """Post a Check Run for the verified commit - the verdict, on the PR,
    where reviewers already look."""
    from verdict.reporter import format_github

    token = os.environ.get(GITHUB_TOKEN_ENV, "").strip()
    if not token:
        raise GitHubError(f"no {GITHUB_TOKEN_ENV} configured - cannot post the check run")
    risk_level = (record.get("risk") or {}).get("level")
    conclusion = check_conclusion(outcome_status, risk_level)
    title = f"{risk_level or outcome_status.upper()}"
    if record.get("risk"):
        r = record["risk"]
        title += f" - {r.get('passed', 0)} passed / {r.get('failed', 0)} failed"
    return _github_request(
        "POST",
        f"{GITHUB_API}/repos/{repo_full_name}/check-runs",
        token,
        {
            "name": "verdict",
            "head_sha": head_sha,
            "status": "completed",
            "conclusion": conclusion,
            "output": {
                "title": title,
                "summary": format_github(record)[:65000],  # Checks API hard limit
            },
        },
    )


def post_pr_comment(repo_full_name: str, pr_number: int, record: dict) -> dict:
    token = os.environ.get(GITHUB_TOKEN_ENV, "").strip()
    if not token:
        raise GitHubError(f"no {GITHUB_TOKEN_ENV} configured - cannot post the PR comment")
    from verdict.reporter import format_github

    return _github_request(
        "POST",
        f"{GITHUB_API}/repos/{repo_full_name}/issues/{pr_number}/comments",
        token,
        {"body": format_github(record)},
    )
