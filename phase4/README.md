# Phase 4 — GitHub integration

**Gate (Section 12): "A real PR on a real repo gets an accurate check without
manual intervention."**

Two trigger paths, one output surface (`verdict/server/github.py` posts the
check run + comment in both):

| Path | Trigger | Needs |
|---|---|---|
| **GitHub Action** (`action/`) | `pull_request` workflow event | one repo secret (`VERDICT_API_KEY`) — nothing else |
| **Webhook + server** (`/webhooks/github`) | GitHub webhook → queue → worker | a reachable server (tunnel), `VERDICT_GITHUB_WEBHOOK_SECRET`, `VERDICT_GITHUB_TOKEN` |

Everything below the live-GitHub boundary is verified by `test_github.py`
(21/21 in `test_results.json`): HMAC signature verification, PR event
parsing, clone + forced checkout of the exact head SHA, the 3-way
exit-code → check-conclusion mapping (LOW→success, risky→failure,
could-not-verify→**neutral**), the markdown check body, the exact Checks API
payload, and the webhook→job→dedupe flow against real Postgres.

## Running the live gate (one manual step)

1. In the repo you want checked: **Settings → Secrets and variables →
   Actions → New repository secret** — name `VERDICT_API_KEY`, value = your
   provider key (e.g. Groq).
2. Copy `action/example-workflow.yml` to `.github/workflows/verdict.yml` in
   that repo (adjust provider/model if not Groq).
3. Open any PR. The `verdict` check appears on it, unaided.

The gate is met when a PR with a real intent-vs-behavior bug gets a
`failure` check with the failing scenario's evidence in the check body, and
a clean PR gets `success`.
