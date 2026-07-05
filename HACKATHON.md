# Verdict — Google Gen AI Hackathon Submission (NVIDIA-powered track)

**Track fit:** *Build a practical data analytics, visualization, or decision-support application that solves a real problem, using two or more of the listed Google Cloud + NVIDIA technologies, with evidence that acceleration improves the experience.*

This is not a hackathon prototype dressed up as a product. It's a real, already-gated verification system ([evidence below](#what-already-exists-built-before-this-submission)), extended for this hackathon into a standing, system-wide vulnerability intelligence layer with an agent on top of it.

---

## The real user and problem

**Who:** engineering managers, tech leads, and platform/security teams responsible for a codebase larger than any one person can hold in their head — specifically ones under pressure from AI-generated code shipping faster than humans can review it.

**The decision that depends on data:** *"Is it safe to merge/release this, and if not, what across our whole system needs attention first?"* Today that decision is made by manually re-reading diffs and trusting reviewer memory. There is no standing, queryable answer to "which of our services currently carry known-vulnerability-class risk, and is it getting better or worse."

**The bottleneck:** verification effort is spent per-PR and thrown away. Nothing accumulates. A vulnerability fixed in one service and reintroduced in another six weeks later looks brand new to every existing tool — because every existing tool has no memory.

---

## Two layers, named on purpose — this is the thesis, not a footnote

**Verdict Core** (already built — see [status](#what-already-exists-built-before-this-submission)): a deterministic pipeline that reads a diff + intent, generates real test scenarios, executes them in a sandbox, and produces an evidence-backed verdict. Not agentic, by design — the LLM proposes, deterministic code checks and runs everything.

**Verdict Intelligence** (this submission): a standing, system-wide vulnerability map built from every Verdict Core run, plus an agent on top of it.

The split is deliberate and it's the answer to the obvious objection before a judge has to ask it: ***why is part of this agentic and part isn't?*** Because they're different jobs. Deciding whether a specific test actually passed is not safe to delegate to an LLM's judgment — that has to be a real, deterministic, sandboxed fact. Correlating findings across a hundred services, prioritizing what to fix first, and noticing that today's bug is a rerun of one from three months ago — that's exactly the kind of multi-step reasoning an agent is good at, and getting it wrong just means a bad suggestion, not a false "your code is safe." Agentic where it's safe. Deterministic where it isn't.

---

## The one moment that proves it

Anyone can demo a scanner finding a bug. The moment that proves a *standing* system-wide map beats a one-shot linter is this:

1. A vulnerability class is planted and fixed in **service A**.
2. Weeks later, the same class is reintroduced in **service B** — via a copy-pasted snippet or a shared-library bump, the way it actually happens in the real world.
3. Live, someone asks the agent: *"have we dealt with something like this before?"*
4. The agent finds the historical case in seconds, cites the original run as evidence, and explains why the two are the same underlying issue.

No per-PR tool can do this — it has no memory of service A by the time it's looking at service B. This is rehearsed until it's boring to run, not improvised on demo day.

---

## Build plan — tiered, so nothing is all-or-nothing

Every phase below is tagged:
- **Tier 1 — must be live and clickable in front of judges.**
- **Tier 2 — should work; strengthens the story if it does.**
- **Tier 3 — built and measured, presented as a committed evidence artifact even if it isn't something a judge clicks live.**

Build order is backbone-first: get a thin, ugly, fully-working slice through every Tier-1 phase before deepening any single one. There is a demoable system from very early on, not seven phases of partial work that only becomes real at the end.

| Phase | Deliverable | Tech | Gate (evidence, not a claim) | Tier |
|---|---|---|---|---|
| 6 | Security-shaped scenario generation, per diff (injection, auth bypass, secret leaks, insecure deserialization, dependency CVEs) | Verdict Core extension (local LLM + deterministic validators) | Precision/recall measured against **real, labeled vulnerability-fix commits** (CVEfixes/BigVul-style dataset), not synthetic fixtures — a judge can be shown the real CVEs it caught | 1 |
| 7 | Standing `vulnerability_map` (service × vuln_class × severity × first/last-seen × status × trend) + durable evidence lake | BigQuery, Cloud Storage | Query "what's open and critical right now" → correct answer, traceable to a specific Cloud Storage evidence bundle | 1 |
| 8 | Historical backfill at real scale + on-demand trend recompute | Managed Service for Apache Spark, RAPIDS Accelerator for Spark, NVIDIA RAPIDS cuDF, NVIDIA GPUs on GCP | Committed benchmark artifact (`hackathon/benchmark/results.json`): identical computation, CPU/pandas baseline vs. RAPIDS, wall-clock + output-equality check, on a real multi-repo dataset (aiming for real scale — hundreds of thousands of commits+, not a demo repo) | 2 (Tier 3 fallback: a smaller but still real, still measured number if full scale doesn't land in time) |
| 9 | Elastic scanning fleet — many repos scanned concurrently | Google Kubernetes Engine | N concurrent repo scans, autoscaled, zero dropped/duplicated jobs — same bar as Verdict Core's existing Phase 3 concurrency gate, just bigger | 2 |
| 10 | Agent: correlate across services, prioritize, cite evidence, request deeper Verdict Core checks, run the "one moment that proves it" demo | Gemini Enterprise Agent Platform | Seeded cross-service/cross-time vuln → agent finds the correct historical link and blast radius, zero unsupported claims (every sentence traces to a real run) | 1 |
| 11 | Passive dashboard — risk ranking, per-service trend, override-rate drift | Looker | Reflects a live BigQuery change within a defined refresh window | 1 |
| 12 | Demo + judge-proofing | — | Backup video recorded, FAQ answers rehearsed, every hackathon-specific claim mapped to a committed file in the repo | 1 |

**Privacy line that stays true throughout:** raw source/diffs never leave the local sandbox + local LLM used for scenario generation. What reaches BigQuery/Looker/Gemini is findings metadata (vuln class, service, severity, trend) — never source code.

---

## Judge-proofing checklist (cheap insurance, don't skip)

- [ ] Full demo recorded end-to-end as a backup — live cloud demos die to wifi/quota/cold-starts more often than to bugs.
- [ ] Rehearsed, crisp answers ready for the three questions a technical judge will ask:
  - *"Is the agent doing real reasoning, or wrapping one LLM call?"* → point to the tool-calling trace and the cross-time correlation demo.
  - *"Is the RAPIDS speedup real, or just I/O-bound?"* → point to `hackathon/benchmark/results.json` — reproducible, output-equality-checked.
  - *"Does source code ever leave the machine?"* → no — point to the architecture diagram and the scenario-gen boundary.
- [ ] Every hackathon-specific claim (precision numbers, speedup numbers, concurrency numbers) has a committed file backing it, the same discipline `phase0/`–`phase5/` already established for Verdict Core.

---

## What already exists (built, gated, before this submission)

This is the actual foundation, with evidence, not claims — and it's the reason this submission is credible instead of just ambitious:

- **Phase 0 — PASSED:** 85% scenario precision (gate: >70%) against 25 real `{diff, intent}` pairs.
- **Phase 1 — PASSED:** full CLI pipeline, 10/10 clean end-to-end runs across real commits.
- **Phase 2 — PASSED (11/11):** Postgres data layer, override tracking, full SQL-explainable verdicts.
- **Phase 3 — PASSED (12/12):** FastAPI + Redis/Celery, 5 concurrent runs, zero drops/dupes, honest degradation when Redis dies.
- **Phase 4 — built, 21/21 local:** GitHub Action + webhook/Checks API, 3-way exit-code contract.
- **Phase 5 — PASSED (9/9):** docker-compose stack, fresh clone → working verdict in 50.3s.

Full detail, evidence files, and an unvarnished "honest status" section (including what's *not* proven yet) live in the main [`README.md`](./README.md).

**What's new for this hackathon:** Phases 6-12 above. Any demo or write-up will be explicit about which technologies are live/measured at Tier 1 versus best-effort at Tier 2/3 — that honesty is part of the pitch, not a hedge.

---

## Why this wins

Most Gen AI hackathon submissions invert this: a flashy agent demo bolted onto no real foundation, with untested claims about scale and speed. This one has the foundation first — a deterministic, gated, evidence-based verification engine that already works — and the agentic, data-intelligence layer is a genuine extension of it, built with the same evidence discipline, not invented for the occasion. The rubric asks for a real pipeline, a useful output, and *evidence* that acceleration helps — this project already treats "evidence over claims" as a habit, and Phases 6-12 apply that habit to BigQuery, Spark RAPIDS, GKE, and an agent instead of inventing it for judging day.
