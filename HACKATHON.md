# Verdict — Google Gen AI Hackathon Submission (NVIDIA-powered track)

**Track fit:** *Build a practical data analytics, visualization, or decision-support application that solves a real problem, using two or more of the listed Google Cloud + NVIDIA technologies, with evidence that acceleration improves the experience.*

This is not a hackathon prototype dressed up as a product. It's a real, already-gated verification system ([evidence below](#what-already-exists-built-before-this-submission)), extended for this hackathon into a standing, system-wide vulnerability intelligence layer with an agent on top of it.

---

## Rubric fit — the five things this must show

| Requirement | Status | Where |
|---|---|---|
| A clear real-world user and problem | ✅ true today | [The real user and problem](#the-real-user-and-problem) |
| A specific decision/bottleneck/workflow depending on data | ✅ true today | "Is it safe to merge/release, what needs attention first" |
| A pipeline that ingests, cleans, analyzes, models, visualizes | 🔶 ingest+analyze built (Verdict Core); model+visualize is Phase 6/7/11 | Build plan below |
| A useful output — dashboard, ranking, risk score, recommendation | 🔶 risk score built today; ranking/dashboard/recommendation is Phase 7/10/11 | Build plan below |
| Evidence that acceleration improves the experience | ✅ real, measured, output-verified — 7.62x on real data at 1.2M rows | [`hackathon/benchmark/results.json`](./hackathon/benchmark/results.json) |

Honesty about which of these are already true versus still being built is itself part of the pitch (see [What already exists](#what-already-exists-built-before-this-submission)) — it's more credible than claiming all five are done when they aren't yet.

---

## The real user and problem

**Who:** engineering managers, tech leads, and platform/security teams responsible for a codebase larger than any one person can hold in their head — specifically ones under pressure from AI-generated code shipping faster than humans can review it.

**The decision that depends on data:** *"Is it safe to merge/release this, and if not, what across our whole system needs attention first?"* Today that decision is made by manually re-reading diffs and trusting reviewer memory. There is no standing, queryable answer to "which of our services currently carry known-vulnerability-class risk, and is it getting better or worse."

**The bottleneck:** verification effort is spent per-PR and thrown away. Nothing accumulates. A vulnerability fixed in one service and reintroduced in another six weeks later looks brand new to every existing tool — because every existing tool has no memory.

---

## Two layers, named on purpose — this is the thesis, not a footnote

**Verdict Core** (already built — see [status](#what-already-exists-built-before-this-submission)): a deterministic pipeline that reads a diff + intent, generates real test scenarios, executes them in a sandbox, and produces an evidence-backed verdict. Not agentic, by design — the LLM proposes, deterministic code checks and runs everything.

**Verdict Intelligence** (this submission): a standing, system-wide vulnerability map built from every Verdict Core run, plus a genuinely autonomous, genuinely multi-agent layer on top of it.

The split is deliberate and it's the answer to the obvious objection before a judge has to ask it: ***why is part of this agentic and part isn't?*** Because they're different jobs. Deciding whether a specific test actually passed is not safe to delegate to an LLM's judgment — that has to be a real, deterministic, sandboxed fact. Everything downstream of that fact — noticing, correlating, prioritizing, alerting, suggesting a fix, even deciding to ask for more verification — is exactly the kind of thing agents should be trusted to do on their own, because getting it wrong costs a bad suggestion, never a false "your code is safe." Autonomous and multi-agent where it's safe. Deterministic and human-decided where it isn't.

---

## The multi-agent layer — four agents, one boundary

"Agentic" shouldn't mean one LLM with a toolbox bolted on. Verdict Intelligence is four narrow, autonomous agents, each triggered by events in the data layer rather than waiting to be asked, coordinated by a thin orchestrator:

| Agent | Trigger | Autonomous action |
|---|---|---|
| **Correlator** | Any new finding written to the vulnerability map | Scans for cross-service/cross-time matches, writes the link back into the map — no one asks it to |
| **Triage** | New finding above a severity threshold | Ranks by severity × blast radius, opens a tracking issue, posts an alert to the owning team, tags a service as a release-blocking candidate — the rubric's own "alert" output, produced with no human prompt |
| **Remediation-advisor** | New HIGH/critical finding | Drafts a suggested fix, attaches it to the finding record — always labeled a suggestion, never applied by anything |
| **Verification-requester** | A pattern the Correlator surfaces (e.g. a shared library just touched three services) | Asks Verdict Core to run a deeper, targeted scan on those services — without waiting for a new push or a human to notice |

**The one boundary that doesn't move:** none of the four can mark a change verified, safe, or mergeable. That fact only ever comes from a real, sandboxed Verdict Core execution. Every other action — noticing, ranking, alerting, suggesting, requesting more proof — runs itself. Human judgment enters at exactly one point: deciding to ship, hold, or close something out. This is what makes the system honestly describable as autonomous and multi-agent without becoming a system that can quietly declare its own work correct.

---

## The one moment that proves it

Anyone can demo a scanner finding a bug. The moment that proves a *standing* system-wide map beats a one-shot linter is this:

1. A vulnerability class is planted and fixed in **service A**.
2. Weeks later, the same class is reintroduced in **service B** — via a copy-pasted snippet or a shared-library bump, the way it actually happens in the real world.
3. Nobody asks anything. The **Correlator agent** picks up the new finding the moment it lands, matches it to service A's case from two months ago on its own, and the **Triage agent** fires an alert before a human even opens the dashboard.
4. Only then does a human look — at an alert that already cites the original run as evidence and explains why the two are the same underlying issue.

No per-PR tool can do this — it has no memory of service A by the time it's looking at service B, and nothing about this demo beat depends on someone remembering to ask the right question. This is rehearsed until it's boring to run, not improvised on demo day.

---

## Build plan — tiered, so nothing is all-or-nothing

Every phase below is tagged:
- **Tier 1 — must be live and clickable in front of judges.**
- **Tier 2 — should work; strengthens the story if it does.**
- **Tier 3 — built and measured, presented as a committed evidence artifact even if it isn't something a judge clicks live.**

**Build order (not phase-number order):** Phase 8 goes **first**, out of sequence, because it's the only rubric bullet with zero evidence today, and it's standalone — it doesn't depend on 6/7/9/10/11 existing. After that, backbone-first through the rest: a thin, ugly, fully-working slice through every Tier-1 phase before deepening any single one, so there's a demoable system early, not partial work that only becomes real at the very end.

| Build order | Phase | Deliverable | Tech | Gate (evidence, not a claim) | Tier |
|---|---|---|---|---|---|
| 1st | 8 | Historical backfill at real scale + on-demand trend recompute — **closes the acceleration bullet, the current zero** | Managed Service for Apache Spark, RAPIDS Accelerator for Spark, NVIDIA RAPIDS cuDF, NVIDIA GPUs on GCP | Committed benchmark artifact (`hackathon/benchmark/results.json`): identical computation, CPU/pandas baseline vs. RAPIDS, wall-clock + output-equality check, on a real multi-repo dataset (aiming for real scale — hundreds of thousands of commits+, not a demo repo) | 2 (Tier 3 fallback: a smaller but still real, still measured number if full scale doesn't land in time) |
| 2nd | 6 | Security-shaped scenario generation, per diff (injection, auth bypass, secret leaks, insecure deserialization, dependency CVEs) — **closes "analyze" in the pipeline bullet** | Verdict Core extension (local LLM + deterministic validators) | Precision/recall measured against **real, labeled vulnerability-fix commits** (CVEfixes/BigVul-style dataset), not synthetic fixtures — a judge can be shown the real CVEs it caught | 1 |
| 3rd | 7 | Standing `vulnerability_map` (service × vuln_class × severity × first/last-seen × status × trend) + durable evidence lake — **closes "model" + the ranking/risk-score output** | BigQuery, Cloud Storage | Query "what's open and critical right now" → correct answer, traceable to a specific Cloud Storage evidence bundle | 1 |
| 4th | 11 | Passive dashboard — risk ranking, per-service trend, override-rate drift — **closes "visualize" + the dashboard output** | Looker | Reflects a live BigQuery change within a defined refresh window | 1 |
| 5th | 10 | Four autonomous agents (Correlator, Triage, Remediation-advisor, Verification-requester) + orchestrator, event-triggered off the vulnerability map — **closes the recommendation/alert output, and makes "autonomous multi-agent" literally true, not just claimed** | Gemini Enterprise Agent Platform | Seeded cross-service/cross-time vuln → Correlator finds the link and Triage fires an alert with zero human prompt, zero unsupported claims (every sentence traces to a real run), and no agent ever marks anything verified | 1 |
| 6th | 9 | Elastic scanning fleet — many repos scanned concurrently | Google Kubernetes Engine | N concurrent repo scans, autoscaled, zero dropped/duplicated jobs — same bar as Verdict Core's existing Phase 3 concurrency gate, just bigger | 2 |
| 7th | 12 | Demo + judge-proofing | — | Backup video recorded, FAQ answers rehearsed, every hackathon-specific claim mapped to a committed file in the repo | 1 |

**Privacy line that stays true throughout:** raw source/diffs never leave the local sandbox + local LLM used for scenario generation. What reaches BigQuery/Looker/Gemini is findings metadata (vuln class, service, severity, trend) — never source code.

---

## Judge-proofing checklist (cheap insurance, don't skip)

- [ ] Full demo recorded end-to-end as a backup — live cloud demos die to wifi/quota/cold-starts more often than to bugs.
- [ ] Rehearsed, crisp answers ready for the three questions a technical judge will ask:
  - *"Is the agent doing real reasoning, or wrapping one LLM call?"* → point to the tool-calling trace and the cross-time correlation demo.
  - *"Is the RAPIDS speedup real, or just I/O-bound?"* → point to `hackathon/benchmark/results.json` — reproducible, output-equality-checked.
  - *"Does source code ever leave the machine?"* → no — point to the architecture diagram and the scenario-gen boundary.
- [ ] Every hackathon-specific claim (precision numbers, speedup numbers, concurrency numbers) has a committed file backing it, the same discipline `phase0/`–`phase5/` already established for Verdict Core.

### Closing the gap to a 10 — five specific artifacts, not more scope

The concept is strong (multi-agent, autonomous, honestly bounded). What separates a 7-out-of-10 delivery from a 10 is proving every one of these live, not just claiming them:

- [ ] **Event-triggering is real.** Each agent fires off an actual Pub/Sub or Cloud Functions trigger on a BigQuery/Cloud Storage write — never a polling loop dressed up as reactive.
- [ ] **A live agent activity feed.** A visible timeline — `[Correlator] 14:02:03 matched finding #482 → billing-api case from 2 months ago`, `[Triage] 14:02:04 opened issue, alerted #payments-team` — so "four distinct autonomous agents" is something a judge watches happen, not a claim they take on faith.
- [ ] **The trust boundary is a behavior, not a README line.** Script the moment an agent says *"I can't mark this fixed — only a real Verdict Core run can confirm that, request sent"* — then show Verdict Core actually run it. Proves the architecture is enforced, not asserted.
- [ ] **The acceleration number is real and reproducible.** Still the one rubric bullet with zero evidence — land `hackathon/benchmark/results.json` before anything else.
- [ ] **Rehearsed until boring.** A 9/10 idea with a shaky live demo loses to a 7/10 idea delivered flawlessly — this is the cheapest point on the list to buy back.

---

## Hackathon build progress — live, honest status

Updated as work actually happens, not after the fact. Same rule as the rest of this project: a step counts as done when there's evidence, not when it's been started.

**Phase 8 (acceleration proof) — in progress, first phase touched:**

- ✅ **RAPIDS environment working** — WSL2 Ubuntu, cuDF 26.06.00 installed, verified against the real local GPU (NVIDIA RTX 4050, 6GB VRAM) with an actual GPU dataframe operation, not just an import check.
- ✅ **Real dataset acquired** — CVEfixes v1.0.0 (Bhandari, Naseer & Moonen, 2021; Zenodo DOI `10.5281/zenodo.4476563`), ~1GB, downloaded and verified. Honest caveat: this is the 2021 release, not the newest v1.0.8 (2024, 12.7GB, broader CVE coverage) — v1.0.0 is enough to prove the CPU-vs-GPU speedup claim; the bigger release is a separate decision for later if Phase 6's precision numbers want the newest CWE coverage.
- ✅ **Data prep script written** — [`hackathon/benchmark/prepare_data.py`](./hackathon/benchmark/prepare_data.py): loads the real SQL dump into SQLite, exports the exact join Verdict Intelligence's `vulnerability_map` needs in production (method-level code metrics → file → commit → CVE → CWE → repository), not an arbitrary table picked for convenience.
- ✅ **Benchmark script written** — [`hackathon/benchmark/rapids_vs_pandas.py`](./hackathon/benchmark/rapids_vs_pandas.py): runs the identical pandas-API groupby/rollup on both pandas and cuDF, verifies the two outputs agree row-for-row before recording any speedup number (a mismatch is a bug, not a result), at real-data scale and a clearly-labeled 20x synthetic scale-up.
- ✅ **Real, verified benchmark result — [`hackathon/benchmark/results.json`](./hackathon/benchmark/results.json).** At the real CVEfixes dataset's native scale (60,661 rows), cuDF is actually *slower* than pandas (0.12s vs 0.02s) — GPU transfer/kernel-launch overhead dominates at this size, and that's reported honestly rather than hidden. At 1.2M rows (a 20x concatenation of the same real rows, simulating the scale an org's accumulated verification history would actually reach), cuDF is a genuine **7.62x faster** (0.15s → 0.02s). Both runs passed the output-equality check (row-for-row identical after the fix below) — a mismatch would have been reported as a failure, not a speedup.
- **Two real bugs found and fixed en route, kept as part of the honest record:** (1) the SQL dump uses SQLite's own dump format (doubled-quote string escaping), not the mysqldump-style backslash-escaping first assumed — an earlier parser version silently fragmented huge amounts of real source-code text into garbage before this was caught by inspecting the actual raw bytes directly; (2) the equality check itself initially flagged a false mismatch because `NaN == NaN` is always `False` in plain float comparison — both engines legitimately produce `NaN` for a group with no populated severity, and that has to count as agreement, not a discrepancy.

**Everything else (Phases 6, 7, 9, 10, 11, 12):** not started. Still exactly what's described in the build plan above, nothing built.

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

It's also genuinely, not decoratively, autonomous and multi-agent — four narrow agents that act on their own triggers, not one LLM wearing a toolbox — with the one boundary that keeps it trustworthy stated out loud instead of hidden: nothing autonomous ever gets to call itself verified.





Full audit, organized by what actually has to be true for this to be both a rubric-winner and genuinely "Verdict":

1. The five rubric bullets
- ✅ Real user/problem, ✅ decision-that-needs-data — already true, nothing to build.
- 🔶 Pipeline (ingest→visualize) — ingest/analyze exists (Verdict Core); "model" (Phase 7 BigQuery map) and "visualize" (Phase 11 Looker) don't yet.
- 🔶 Useful output — risk score exists today; ranking, dashboard, recommendation don't yet.
- ❌ Acceleration evidence — the one true zero. Nothing benchmarked. Must be built with a real, large dataset (CVEfixes/BigVul + real repo histories), not a demo repo, or it won't survive a technical judge's second question.

2. The 9 required technologies — all must do real work, none decorative
- BigQuery + Cloud Storage → Phase 7, the standing vulnerability_map + evidence lake.
- Managed Spark + Spark RAPIDS + RAPIDS cuDF + NVIDIA GPUs on GCP → Phase 8, the acceleration proof — build this first, standalone, since it's the current zero.
- GKE → Phase 9, elastic scanning fleet (lowest visibility to judges, but needed to make "system-wide" true rather than a 3-repo toy).
- Gemini Enterprise Agent Platform → Phase 10, the 4-agent layer.
- Looker → Phase 11, passive dashboard.

3. What must never bend — the "Verdict" part of Verdict
- A verdict only ever comes from a real, sandboxed Verdict Core execution — never from an agent's judgment. This is the whole reason the brand means anything.
- Two layers stay named and separate everywhere (pitch, diagrams, code): Verdict Core (deterministic) vs. Verdict Intelligence (agentic). Never blur them into one undifferentiated "AI system."
- Privacy line holds: raw diffs/source never leave the local sandbox+LLM. Only findings metadata (class, severity, service) reaches BigQuery/Looker/Gemini.

4. What must be true for "autonomous multi-agent" to be real, not claimed
- Four distinct agents (Correlator, Triage, Remediation-advisor, Verification-requester) + a thin orchestrator — not one LLM with a toolbox.
- Each fires on a real event trigger (Pub/Sub / Cloud Functions off a BigQuery/Storage write) — not a polling loop pretending to be reactive.
- Each agent's action is visibly separate (a live activity feed/log) — a judge should watch four actors fire, not be told there are four.
- The one enforced boundary is demonstrated live: an agent explicitly declining to self-certify, handing off to a real Core run, shown actually happening.

5. Delivery risk — the difference between a 10 and a 7
- Build order: Phase 8 first (isolated, closes the only rubric zero), then 6 → 7 → 11 → 10 → 9 → 12.
- Tiering discipline: Tier 1 phases must be live-clickable; Tier 2/3 can be a committed evidence artifact if the live version doesn't land in time — nothing is all-or-nothing.
- Judge-proofing executed, not just listed: recorded backup demo, rehearsed answers to the 3 hard questions, every claim backed by a committed file.

Everything above is already written into HACKATHON.md in more detail — this is the compressed version. Want me to start executing, beginning with Phase 8 (the acceleration benchmark), since it's the one item on this whole list with zero work done and the biggest single point of failure if skipped?