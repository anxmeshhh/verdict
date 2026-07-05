# Verdict Intelligence — plan (no cloud requirement)

Not a hackathon submission. This is the working plan for extending Verdict Core into a standing, autonomous vulnerability-intelligence layer, using only what's already in the stack (Postgres, the existing pluggable LLM provider, a local web UI) - no GCP/NVIDIA-specific tech required.

## The thesis (unchanged from earlier design work)

**Verdict Core** (built, gated — see main [`README.md`](./README.md)): a deterministic pipeline that reads a diff + intent, generates real test scenarios, executes them in a sandbox, and produces an evidence-backed verdict. Not agentic, by design.

**Verdict Intelligence** (this plan): a standing, system-wide vulnerability map built from every Verdict Core run, plus a genuinely autonomous, multi-agent layer on top of it.

The split stays the same and for the same reason: deciding whether a test actually passed is a deterministic, sandboxed fact - not safe to delegate to an LLM. Correlating findings, prioritizing what to fix, and deciding to ask for a deeper check are exactly the kind of things an agent can be trusted to do on its own, because getting it wrong costs a bad suggestion, never a false "your code is safe." **The one boundary that never moves: no agent can mark a change verified. That only ever comes from a real Verdict Core sandbox run.**

## What's already real (done, not re-litigated)

- **Phase 6 (security-shaped scenario generation)** — `verdict/generator.py`, `verdict/validator.py`, `verdict/depcheck.py`, `verdict/findings.py`. Scenario-gen now proposes security-shaped scenarios (injection, auth_bypass, secret_leak, insecure_deserialization), guarded by validator checks, plus a deterministic OSV.dev dependency-CVE checker. Wired into the pipeline; findings written to `.verdict/findings/<run_id>.json`.
- **Real precision/recall measured** — [`hackathon/security_gen_eval/`](./hackathon/security_gen_eval/): 20 real CVEfixes commits, ~75-80% recall, two real validator/mapping bugs found and fixed from direct evidence (not guessed at). This dataset-driven validation approach was good and stays.
- **A real CPU-vs-GPU benchmark** — [`hackathon/benchmark/`](./hackathon/benchmark/): kept as a completed, verified artifact. Not a dependency of the shipped product going forward (Verdict doesn't need RAPIDS/GPU acceleration to work) - it stays as evidence that the earlier acceleration claim was real, nothing more.

## What changes (dropping the cloud-specific requirements)

| Was planned as | Now | Why |
|---|---|---|
| BigQuery `vulnerability_map` | **Postgres** — a `findings` table added to the existing `verdict/store.py` schema | Postgres + dual-write is already built (Phase 2) and already the project's data layer; no new infra |
| Cloud Storage evidence lake | **Local filesystem** (`.verdict/findings/`, already built) | Already durable, already there |
| Managed Spark + Spark RAPIDS + GPU backfill | **Dropped** | Was only ever there to prove GPU acceleration for a rubric bullet that no longer applies |
| GKE elastic fleet | **Dropped** | Not needed for a local, single-user tool |
| Gemini Enterprise Agent Platform | **Whatever LLM provider is already configured** (Groq today, swappable like everything else in `verdict/llm.py`) with tool-calling | Same agent behavior, no GCP-specific product dependency |
| Looker dashboard | **A local web UI**, served from Verdict's existing FastAPI server (Phase 3) | Already have a server; this is a page on it, not new infra |

## The three things left to build

### 1. Fully autonomous multi-agent layer

Same four-agent design as before, unchanged in spirit:

| Agent | Trigger | Autonomous action |
|---|---|---|
| **Correlator** | New finding written | Matches it against past findings (cross-service/cross-time), links them |
| **Triage** | New finding above severity threshold | Ranks by severity, surfaces an alert - no human prompt |
| **Remediation-advisor** | New HIGH/critical finding | Drafts a suggested fix, attached as a suggestion, never applied |
| **Verification-requester** | A pattern the Correlator surfaces | Asks Verdict Core to run a deeper scan - the one path back to real proof |

Runs locally: triggered right after `verdict check`/`run` writes a new finding (in-process, no Pub/Sub needed for a single-machine tool), using the already-configured LLM provider for reasoning with function-calling into Postgres (read-only).

### 2. A UI to display it

A real page, not a JSON file: risk ranking across whatever's been scanned, per-finding detail, and a live view of the agents acting (the "agent activity feed" concept) - served from the existing FastAPI app (`verdict serve`) rather than a new framework.

### 3. Setup polish

Extend `verdict init`/`verdict db init` so turning on Verdict Intelligence is one command, not a manual multi-step process - consistent with how the rest of Verdict Core already onboards (`verdict init` asks, sets up, done).

## Build order

1. ✅ `findings` table in Postgres (`verdict/store.py`) + wire `verdict/findings.py` to dual-write there, same pattern as `runs`/`audit_log`. Live-verified against the real dev Postgres fixture.
2. ✅ **Correlator + Triage agents — real, working, autonomous.** `verdict/agents/correlator.py` and `verdict/agents/triage.py`, triggered automatically the moment `findings.save()` runs, no human prompt. Live-verified end to end with real Groq calls: two differently-worded findings planted across two simulated services (`billing-api`, `notifications-service`) were correctly matched by the Correlator as the same underlying SQL-injection pattern, with a real reasoning string, then Triage fired a real console alert and wrote it to a durable local log. Known, documented limitation: correlation match/no-match can vary run-to-run (same LLM non-determinism caveat as Phase 6's eval) - a missed match this run doesn't mean the wiring is broken, both findings still alert correctly on severity alone either way.
3. ✅ UI: `/intelligence` on the existing FastAPI server (`verdict/server/api.py`) - risk ranking by repo, full finding list with correlation/status/suggested fix, read-only, live-verified with real data via FastAPI's TestClient (200 OK, correct rendering, HTML-escaping of free-form LLM text verified safe against injection).
4. ✅ Setup: `verdict db init` already provisions the `findings` table (same `SCHEMA` constant as everything else, plus `ADD COLUMN IF NOT EXISTS` for the two columns added in step 5 below so existing databases pick them up without a manual migration) - no new command needed. `verdict health` now reports an `intelligence` line (open/alerted finding counts), verified live.
5. ✅ **Remediation-advisor + Verification-requester — the last two agents, real and working.** `verdict/agents/remediation.py` drafts a concrete, vuln_class-specific fix suggestion (via a real Groq call) for any HIGH/CRITICAL finding, stored in a new `suggested_fix` column - always a suggestion, never applied. `verdict/agents/verifier.py` is pure deterministic logic (no LLM) - when the Correlator matches a new finding to a past one, it flags the *older* finding's `reverification_reason` column, since the same pattern resurfacing elsewhere is a signal the original fix may not have held. Deliberately scoped to flagging, not auto-re-scanning: this agent doesn't have a checked-out copy of whatever repo the older finding came from, so it makes the request visible rather than fabricating access it doesn't have.

**All four agents from the original design are now real, wired, and live-verified end to end:** a live test planting two similar SQL-injection findings across two simulated services produced, autonomously, with zero human prompt: a correct cross-service match (Correlator), a fired alert (Triage), a concrete fix suggestion on both findings (Remediation-advisor), and a reverification flag on the older one (Verification-requester).
