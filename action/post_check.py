"""Post the verdict as a Check Run (and optionally a PR comment) from inside
a GitHub Actions run.

Usage: post_check.py <verdict-result.json> <exit_code>

Reuses the exact same posting code the webhook path uses
(verdict.server.github) - one output surface, two triggers. Exit-code
contract -> check conclusion: 0 LOW -> success, 1 risky -> failure,
2 could-not-verify -> neutral (alert the checker's owner, don't blame the
code).
"""
import json
import os
import sys


def main() -> int:
    result_path, exit_code = sys.argv[1], int(sys.argv[2])

    from verdict.server import github as gh

    # The Actions-provided token drives the same code path as the server's.
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        os.environ.setdefault(gh.GITHUB_TOKEN_ENV, token)

    event_path = os.environ.get("GITHUB_EVENT_PATH", "")
    repo_full = os.environ.get("GITHUB_REPOSITORY", "")
    if not (event_path and repo_full and os.path.exists(event_path)):
        print("no GitHub event context - skipping check post")
        return 0
    event = json.load(open(event_path, encoding="utf-8"))
    pr = event.get("pull_request")
    if not pr:
        print("not a pull_request event - skipping check post")
        return 0

    try:
        record = json.load(open(result_path, encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        # Even a crashed run must leave a visible trace on the PR.
        record = {
            "run_id": "unknown", "status": "errored", "failed_stage": "action",
            "reason": f"verdict produced no readable result ({e})", "risk": None,
        }

    status = record.get("status", "completed")
    if exit_code == 2 and status == "completed":
        status = "errored"  # trust the exit code if the two ever disagree

    try:
        gh.post_check_run(repo_full, pr["head"]["sha"], record, status)
        print(f"check run posted: {gh.check_conclusion(status, (record.get('risk') or {}).get('level'))}")
    except gh.GitHubError as e:
        print(f"::warning::could not post check run: {e}")

    if os.environ.get("POST_COMMENT", "true").lower() == "true":
        try:
            gh.post_pr_comment(repo_full, pr["number"], record)
            print("PR comment posted")
        except gh.GitHubError as e:
            print(f"::warning::could not post PR comment: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
