# Verdict

An independent, self-hosted pre-deployment verification system for AI-generated and human-written code.

Verdict reads a code diff plus its stated intent, and proves — through generated, executed tests — whether the change actually does what it claims, before a human reviews it. It is not another AI coding assistant; it is the neutral referee that sits downstream of tools like Cursor, Claude Code, and Antigravity, verifying their output rather than producing more of it.

Full original vision doc (architecture rationale, worked examples, design discussion): [`Verifier_Project_Direction.docx`](./Verifier_Project_Direction.docx). This README tracks current, living status — the doc predates the project's rename from "Verifier" to "Verdict" and won't reflect progress made after it was written.

Not agentic, by design: Verdict is a **deterministic pipeline with exactly two narrow, bounded LLM steps** (scenario generation, then turning each scenario into runnable test code). Every other stage — intent extraction, validation, sandbox execution, risk scoring, reporting — is pure deterministic logic, and neither LLM step is trusted blindly (see [Where the LLM touches this](#where-the-llm-touches-this-and-where-it-doesnt) below). Autonomous agent loops are unpredictable and hard to audit, a bad fit for a tool whose entire value is trustworthiness.

## The workflow

```
┌─────────────────────────── ONE-TIME SETUP ────────────────────────────┐
│                                                                        │
│  git clone <friend's project>                                        │
│         │                                                             │
│         ▼                                                             │
│  verdict init                                                         │
│         │                                                             │
│         ├── run bare ────────────► asks: local Ollama or cloud?       │
│         │                            (enter = local; cloud = pick      │
│         │                            provider, paste key, pick from   │
│         │                            the real model list it fetches)  │
│         │                                                              │
│         └── already know what you want? ─► verdict init --provider X  │
│                                              --model Y --api-key Z     │
│         │                                                              │
│         ▼ (want to switch it later instead?)                          │
│  verdict model  ──────►  same interactive picker, any time after init │
│         │                                                             │
│         ▼                                                             │
│  verdict install-hook  ──────►  every future push auto-checks         │
│                                                                        │
└────────────────────────────────────────────────────────────────────────┘

┌────────────────────────── DAILY LOOP ─────────────────────────────────┐
│                                                                        │
│  Write code (with or without an AI assistant)                        │
│         │                                                             │
│         ▼                                                             │
│  State intent — one line in .verdict/INTENT.md or the commit message  │
│         │                                                             │
│         ▼                                                             │
│  verdict check   (no flags — auto-detects what to verify)             │
│         │                                                             │
│         ▼                                                             │
│  Verdict generates real scenarios → runs them in a sandbox            │
│         │                                                             │
│         ├──── LOW RISK ─────────────► push with confidence            │
│         │                                                             │
│         └──── HIGH/MEDIUM ────► read the failing scenario + evidence  │
│                       │                                               │
│                       ▼                                               │
│              fix the actual bug it found                              │
│                       │                                               │
│                       └──────────────► back to `verdict check`        │
│                                                                        │
└─────────────────────────────────────────────────────────────────────────┘

┌───────────────────────── SAFETY NET (if they forget) ───────────────────┐
│                                                                         │
│  git push                                                              │
│      │                                                                 │
│      ▼                                                                 │
│  pre-push hook fires: verdict run --base ... --ref ...                 │
│      │                                                                 │
│      ├──── exit 0 (LOW) ──────────► push goes through                  │
│      │                                                                 │
│      ├──── exit 1 (risky: MEDIUM/HIGH/UNVERIFIED) ──► push BLOCKED     │
│      │            "your code looks risky"                             │
│      │                                                                 │
│      └──── exit 2 (couldn't verify: bad ref, provider down) ──► push   │
│                   BLOCKED — "could not verify this push (a checker     │
│                   problem, not necessarily your code)"                 │
│                       │                                                │
│                       ├── fix it and try again, or                    │
│                       └── git push --no-verify (visible, deliberate    │
│                           override — git's own mechanism, always       │
│                           works this way, not something Verdict        │
│                           controls)                                    │
│                                                                         │
└───────────────────────────────────────────────────────────────────────────┘
```

## Quick reference for new users

The whole surface a new user needs for the first few weeks — `verdict check` day to day, `logs`/`report` when something's flagged, `install-hook` once so verification is never optional to remember. Everything else (server mode, Postgres, GitHub Actions, `db`/`serve`/`worker`) is for later, once you're running this as a team, not solo — see [Everyday commands](#everyday-commands) below for the full surface.

**Setup (once per project)**

| Command | What it does |
|---|---|
| `verdict init` | First-time setup. Bare = asks local Ollama (private, free) or cloud; already know what you want? `--provider`/`--model`/`--api-key` skip the prompt. |
| `verdict model` | Same interactive picker, any time after `init` — switch provider, paste key, pick from the real model list. |
| `verdict install-hook` | Makes every future `git push` auto-verify itself. Optional but recommended. |

**The daily loop**

| Command | What it does |
|---|---|
| `verdict check` | The one to actually remember. No flags — figures out what to verify on its own. |
| `verdict watch` | Live mode — verifies automatically once the working tree goes quiet for a few seconds (not on every keystroke — waits for a natural pause so it never fires mid-edit). |
| `verdict scenario add` | Write your own check in plain English if you want to test something specific yourself — no YAML. |

**When something's flagged**

| Command | What it does |
|---|---|
| `verdict logs last` | The full evidence — the actual generated test and what happened when it ran. |
| `verdict report last` | A clean, shareable HTML page of the same thing, for a teammate or a PR. |

**Looking back**

| Command | What it does |
|---|---|
| `verdict runs` | History of everything you've checked, as a table. |
| `verdict health` | "Is everything actually working right now" — provider, Docker, disk. |

**The safety net**

| Command | What it does |
|---|---|
| `verdict use <name>` | Switch between saved provider setups by name — never retype a key. |
| `git push --no-verify` | Deliberately skip the check for this one push (git's own mechanism, always visible in your history). |

## Status

**Phase 0: PASSED.** 85% precision (gate was >70%) — see [`phase0/`](./phase0) for the script, dataset, and evidence. 25 real `{diff, intent}` pairs pulled from actual commit history, scenarios generated via local `qwen2.5-coder:7b`, manually judged for traceability against the real diff content.

Key finding from Phase 0: scenario quality was excellent (mostly 100%) on commits with clear, descriptive intent, and degraded badly on vague/placeholder commit messages (e.g. "fix in the rapidapi or ytdlp"). This means **Module 2 (Intent Extractor) needs real vagueness detection**, not just pass-through — a design requirement discovered empirically, not assumed upfront.

**Phase 1: PASSED.** Full pipeline as a CLI tool — all 8 modules built and verified module-by-module. Gate evidence in [`phase1/gate_results.json`](./phase1/gate_results.json): 10/10 end-to-end runs across 10 different real commits, zero crashes. The gate failed twice first and earned its keep both times — attempt 1 caught wrong abort semantics (zero-evidence runs now finish as recorded UNVERIFIED verdicts) and a Windows console crash; attempt 2 caught a nondeterministic crash when LLM-generated tests print unicode through a cp1252 pipe (fixed at the stream level: output can never kill a verdict).

Beyond the doc's Phase 1 scope, also shipped: interactive `verdict` shell, append-only audit log (`.verdict/audit.jsonl`), token accounting on every LLM call, hybrid mode (`--hybrid`), `config get/set`, and a git pre-push hook (`verdict install-hook`) that verifies exactly the commits leaving the machine and blocks non-LOW pushes.

**Post-Phase-1 additions (2026-07-03):**
- **Pluggable LLM providers** — Ollama stays the default (local, private), but one `config set provider` away are OpenRouter, Groq, Gemini, OpenAI, or any OpenAI-compatible endpoint (`custom` + `base_url`, which is also the vLLM scale-out path). API keys are masked everywhere they surface — screen and audit log — and `verdict init`/`verdict model` add a `.verdict/` entry to `.gitignore` automatically (creating the file if it doesn't exist yet) so the full diffs, prompts, and raw LLM responses under `.verdict/cache/`, `.verdict/runs/`, and `.verdict/audit.jsonl` never get swept up by a plain `git add -A`. Switching to a cloud provider prints an explicit privacy warning: diffs leave the machine. Validated end-to-end against a mock OpenAI-compatible server: health check, scenario generation, test generation, token accounting, sandbox execution, verdict.
- **`verdict watch`** — live pre-deployment mode. Watches the working tree, knows the difference between "mid-generation" and "settled" (fingerprint debounce; `.verdict/` excluded so its own records never re-trigger it), and fires the full pipeline only after the dust settles. Intent comes from `--intent` or a live-editable `.verdict/INTENT.md`. Validated: activity → settle → verify → verdict → back to watching, no duplicate triggers.
- **Full scope control** — `--path <file-or-folder>` (repeatable) on `run`, `plan`, and `watch`: verify exactly the files the developer chooses, nothing else.
- **Human-readable history** — `verdict runs` (table of past verdicts), `verdict report [run-id]` (self-contained, shareable HTML page per run), and `'last'` works anywhere a run id does. The JSON records stay on disk as evidence; no human has to read them.

**Phase 2: PASSED (2026-07-04).** Postgres data layer (dual-write; the file store stays canonical for the plain CLI), `verdict override <id> --reason` with the override rate tracked as a first-class metric, `verdict status`, `verdict db init/migrate-files/stats`. Gate MET 11/11 ([`phase2/gate_results.json`](./phase2/gate_results.json)): the real 66-run history from live testing migrated, then a real HIGH-risk run fully explained — reasons, failing-scenario evidence, test code, exact prompt — from SQL alone.

**Phase 3: PASSED (2026-07-04).** FastAPI gateway + Redis/Celery worker pool + Module 18 health (`verdict serve` / `verdict worker`; `/health`, `/metrics` in Prometheus format). Gate MET 12/12 ([`phase3/gate_results.json`](./phase3/gate_results.json)): 5 concurrent runs through the real stack in 11.6s, zero dropped/duplicated (dedupe enforced by a Postgres UNIQUE constraint, not app logic), LLM-down jobs park as `waiting_on_llm` and retry, Redis stopped mid-gate → honest 503 + new work refused + clean recovery.

**Phase 4: BUILT, local checks 21/21 (2026-07-04).** GitHub Action wrapper ([`action/`](./action)) + webhook/Checks API path ([`verdict/server/github.py`](./verdict/server/github.py)). The 3-way exit-code contract lands on PRs as check conclusions: LOW→success, risky→failure, could-not-verify→**neutral** (checker problem ≠ code risk). Live gate needs one repo secret — see [`phase4/README.md`](./phase4/README.md).

**Phase 5: PASSED (2026-07-04).** docker-compose stack (Postgres, Redis, API, worker, optional Ollama profile) + `setup.sh`/`setup.ps1`. Gate MET 9/9 ([`phase5/gate_results.json`](./phase5/gate_results.json)): a literal fresh `git clone` → `.env` → `compose up --build` → submitted run → LOW verdict through the full stack in **50.3 seconds** against the 10-minute budget.

**Scenario-level concurrency (2026-07-04).** `run`/`check`/`watch` now execute up to `--sandbox-concurrency` (default 3) sandbox containers at once instead of one at a time — each scenario is fully isolated (own container, own scratch dir), so nothing shares state. The saved record's scenario order stays deterministic regardless of which container finishes first; only live progress lines reflect actual completion order. Combines with Phase 3's job-level concurrency (multiple runs at once); the two multiply against each other, so a worker's total containers = concurrent jobs × sandbox concurrency — pick `--sandbox-concurrency` with that in mind on a shared worker.

Manual production-readiness checklist for everything above: [`TESTING.md`](./TESTING.md).

## Phased roadmap

| Phase | Deliverable | Gate to proceed |
|---|---|---|
| 0 | ✅ Offline precision validation, no infra | Precision > 70% — **85%** |
| 1 | ✅ Core pipeline as a CLI tool | Runs cleanly end-to-end, 10x in a row, no crashes — **10/10** |
| 2 | ✅ Control & trust layer — config, override, logs, Postgres | "Why did it flag this" from stored data alone — **11/11** |
| 3 | ✅ Concurrency & reliability — Redis queue, worker pool, health checks | 5 concurrent runs, no dropped/duplicated jobs — **12/12** |
| 4 | ✅* GitHub integration — webhook, Checks API, Action wrapper | A real PR gets an accurate check, unaided — *local 21/21; live run needs a repo secret* |
| 5 | ✅ Packaging — docker-compose, setup script | A stranger clones it and gets a working run in <10 min — **50.3s** |
| 6 | Public validation — publish precision/recall numbers | Real external users, real override data |
| 7 | Dashboard — read-only web UI | Preferred over raw logs for daily use |
| 8 | Real-time layer — WebSocket, settlement-based live triggering | Live mode <5s/save, doesn't compete with an active agent |
| 9 | IDE extension — inline decorations | Matches dashboard/CLI exactly, no drift |
| 10 | Scale hardening — Firecracker, vLLM, fine-tuning | Only if metrics prove a bottleneck |

Phase 1 is already a real, usable tool even if the project stopped there. Phase 5 is the point a stranger can install and trust it. Everything past Phase 6 is additive reach.

**Recorded for later (deliberately not built yet):** a `verdict.yaml` project-runner declaration (services/test-command/seed data — what full-stack repos need the sandbox to stand up), diff chunking for very large PRs, and per-service sandbox secrets.

## Module breakdown (Phase 1 scope: Modules 1-8)

| # | Module | Input | Output |
|---|---|---|---|
| 1 | Config & Setup | None (first run) | Local stack ready (Ollama reachable) |
| 2 | Intent Extractor | Raw diff + PR description/commit message | `{diff, intent}`, flagged if intent too vague |
| 3a | Scenario Generator (autonomous) | `{diff, intent}` | LLM-generated scenario list (JSON) |
| 3b | Scenario Authoring (manual) | Developer-written YAML/JSON | Developer scenarios, same schema |
| 4 | Scenario Validator | Scenarios from 3a/3b | Schema + traceability check against diff lines |
| 5 | Sandbox Runner | Validated scenarios + repo at commit SHA | Pass/fail + evidence per scenario (Docker) |
| 6 | Risk Scorer | Sandbox results | Risk level per file (LOW/MEDIUM/HIGH) |
| 7 | Reporter | Risk scores | Formatted output: terminal/GitHub/JSON |
| 8 | CLI | User command | Full staged, live pipeline output |

## Where the LLM touches this (and where it doesn't)

The engine is deterministic logic, not the model. Grepping the whole codebase for LLM call sites turns up exactly two files that ever talk to a model — `verdict/generator.py` (scenario generation) and `verdict/testgen.py` (turning a scenario into executable test code). Everything else, including everything added in Phases 2-5, is plain code:

| Module | Touches the LLM? |
|---|---|
| Intent extraction (diff parsing, vagueness check) | No — pure logic |
| **Scenario generation** | **Yes** — the model proposes what's worth testing |
| Scenario validation (traceability, hallucination guards) | No — pure logic, actively distrusts the LLM's output |
| **Testgen** (scenario → executable test code) | **Yes** — the model writes the check |
| Sandbox execution (Docker) | No — pure orchestration |
| Risk scoring | No — deterministic pass/fail/coverage math |
| Reporter, CLI, data layer, health checks, API gateway, queue, GitHub integration | No — all pure logic |

Neither LLM step is trusted blindly — a proposal, not a verdict:
- The **validator** independently checks every LLM-proposed scenario against the actual diff lines before it's allowed to run at all.
- Several **static hallucination guards** (dead-function detection, broken-monkeypatch detection, unsupported-behavior-claim checks for type-enforcement/logging/thread-safety/format-validation claims) catch specific patterns where the model asserts something the code doesn't actually do.
- The **confirm-FAILED pass** independently regenerates and re-runs any failing test before trusting the failure, so a flaky generated test can't masquerade as a real bug.

This is the whole "not agentic" pitch: the LLM never decides what a verdict *is* — it only feeds two well-checked inputs into a pipeline a human could audit line-by-line without ever needing to trust the model's judgment.

## Tech stack

| Layer | Technology |
|---|---|
| LLM runtime | Ollama (local default) or any OpenAI-compatible API — OpenRouter/Groq/Gemini/OpenAI/custom (vLLM at scale) |
| Model | `qwen2.5-coder:7b` (default) |
| API (post-Phase-1) | FastAPI (Python, async-native) |
| Queue (Phase 3+) | Redis + Celery |
| Sandbox | Docker (v1) → Firecracker (scale) |
| Database (Phase 2+) | Postgres |
| Artifacts (Phase 2+) | MinIO (S3-compatible, self-hosted) |
| Dashboard (Phase 7+) | Next.js |
| Real-time (Phase 8+) | WebSocket server + Redis Pub/Sub |
| IDE surface (Phase 9+) | VS Code extension |
| Observability | Prometheus + Grafana |
| Distribution (Phase 5+) | Docker Compose + setup script + GitHub Action |

## LLM setup

Local by default: Verdict's scenario generation runs against a self-hosted [Ollama](https://ollama.com) instance, so diffs never leave the machine.

- Model: `qwen2.5-coder:7b`
- Run `ollama serve` to start the local inference server before running Verdict.

Prefer a hosted model? Pick the provider right at setup, in one shot:

```
verdict init --provider groq --model llama-3.3-70b-versatile --api-key <key>
# or: openrouter | gemini | openai | custom (custom also needs --base-url)
```

Don't know the exact model id? `verdict model` (or `/model` in the interactive shell) walks
you through it: pick a provider, paste the API key, and it fetches the real list of models
that key can use right now and lets you pick from it - never a hardcoded or guessed name.

All providers go through one OpenAI-compatible transport, and you can change any of it later without re-running init:

```
verdict config set provider openrouter   # or: groq | gemini | openai | custom
verdict config set model  <model-id>
verdict config set api_key <key>         # or set VERDICT_API_KEY (masked in logs either way)
verdict config set base_url <url>        # only for provider=custom (vLLM, LM Studio, ...)
```

Verdict warns loudly when a cloud provider is active: your diffs and intents leave the machine.

### Scenario-gen caching

Scenario generation (the one LLM step) is cached under `.verdict/cache/scenario_gen/`,
keyed on the exact prompt + model. The same commit re-run against the same model returns
the same scenario set instead of re-asking the model - cloud providers pin `temperature=0`/
`seed=0` but still aren't guaranteed bit-exact (shared, batched inference), so caching is
what actually delivers "same commit -> same scenario set," not the sampling params alone.

This is scoped to scenario-gen only - validate/testgen/execute always run fresh against
whatever scenario came out of the cache, so a stale cached scenario still gets caught by
current validator logic. Test execution itself is never cached: a PASSED must mean the
code was actually run *this* time, not replayed from a previous run.

Pass `--force-regenerate` to `run`/`plan` to bypass the cache and ask the model fresh -
use it whenever you're deliberately testing model reliability (e.g. hunting an intermittent
hallucination), since a cache hit would otherwise hide it.

## Everyday commands

The full command surface with flags. New here? [Quick reference for new users](#quick-reference-for-new-users) above covers everything you need for the first few weeks.

```
verdict                          # branded interactive shell
verdict check                    # verify the obvious thing - no flags (uncommitted changes, else last commit)
verdict run                      # verify HEAD (or --ref, --base, --intent for working tree)
verdict run --path src/auth/     # verify only the files/folders you choose (repeatable)
verdict run --sandbox-concurrency 5   # run more scenario containers at once (default 3, 1 = sequential)
verdict watch                    # live mode: auto-verify when the working tree settles
verdict scenario add             # author a scenario interactively - no YAML to learn
verdict use groq                 # switch provider profiles by name, no secrets typed
verdict runs                     # history of past verdicts as a table
verdict report last              # shareable self-contained HTML page for a run
verdict logs last                # full evidence: prompt, test code, sandbox output
verdict override run_x --reason "..."   # disagree with a verdict, on the record
verdict install-hook             # pre-push gate: non-LOW verdicts block the push
verdict health                   # honest liveness of every configured dependency
```

**Exit codes (CI contract):** `0` verified LOW · `1` the code looks risky (MEDIUM/HIGH/UNVERIFIED) · `2` verdict itself couldn't verify (bad ref, provider down — alert the checker's owner, don't blame the code).

## Server mode (Phases 2-3)

```
./setup.sh          # or .\setup.ps1 on Windows - writes .env, builds, starts, waits for health
```

Brings up Postgres + Redis + API (`:8400`) + worker. `POST /runs` to submit, `GET /jobs/<id>` to watch stage progression, `GET /runs/<id>` for the verdict, `/health` and `/metrics` (Prometheus) for operations, and a read-only run-history page at `/`. Same-commit submissions dedupe; `verdict override` and run history read from Postgres. Repos to verify live under `./data/repos` (the sandbox reaches them through the host Docker daemon).

## GitHub PRs

Copy [`action/example-workflow.yml`](./action/example-workflow.yml) to `.github/workflows/verdict.yml` in the repo you want checked, add a `VERDICT_API_KEY` repo secret, open a PR — the `verdict` check appears with per-scenario evidence. A webhook + Checks API path for the self-hosted server ships too ([`phase4/README.md`](./phase4/README.md)).

## Honest status (2026-07-04)

Not a pitch — an evidence-based read of where this actually stands, caveats included, because a tool whose whole job is delivering hard truths about code should be able to take one about itself.

**Is the need real?** Yes, and it's specific, not vague. Static analysis, linting, and type-checkers already catch syntax and type errors. The gap is code that *reads* correctly but doesn't do what it claims — exactly the class of bug planted and tested against all session (a rate limiter keyed by IP instead of account: compiles fine, looks reasonable in a diff, silently wrong). Traditional tooling doesn't catch that category, and it's the category growing fastest as more code is AI-generated. An independent, self-hosted referee between "AI wrote this" and "a human approved this" is solving a real, current problem.

**What our own testing actually shows:**
- The planted bug was caught reliably, across dozens of runs, on two different LLM backends (a small local model and a much larger cloud one) — not a lucky first run, but consistent under repeated, adversarial pressure.
- The phase-gate discipline is real, not performative: gates have numeric bars, and some failed before passing (Phase 1 failed twice before 10/10; Phase 3 initially failed on a real 87%-full disk before the health-check threshold was corrected). The team's own tool was allowed to say "not yet."
- Every bug found this session was fixed and then independently re-verified with a real repro, not just claimed fixed — see [`TESTING.md`](./TESTING.md) Section 16 for the two most recent (a `check` false-positive on untracked-only files, and testgen conflating a rate-limited provider with a genuinely un-sound scenario — both closed same-day).
- Every serious defect found all session lived in the deterministic scaffolding around the one LLM step, never "the AI is just unreliable, nothing to be done." That's the difference between a tool that's fundamentally trustworthy with fixable rough edges, and one that's fundamentally a coin flip.

**The honest caveats:**
- The one bounded LLM step still occasionally hallucinates or makes wrong assumptions (e.g. guessing a function's argument order instead of checking its signature — found and patched this session, see Section 3c of `TESTING.md`). Surrounding deterministic checks catch most of this, but it's an ongoing arms race, not a solved problem.
- Everything validated so far is on a small, controlled demo repo built specifically to contain a known bug. That's the right way to validate the *mechanism* — it is not yet proof this holds up on a messy, large, polyglot production codebase. The roadmap says so itself: Phase 6 is explicitly "real external users, real precision/recall data," and it hasn't happened yet.
- The full-stack story — multi-service sandboxes, diff chunking for huge PRs, per-service secrets — is explicitly not built (see "Recorded for later" above). Pointed at a large microservices monorepo today, expecting it to work the way it does on this two-file demo would be the wrong expectation.

**How it actually helps, concretely:** in development, it moves "does this actually work" to the moment code is written rather than the moment an overloaded reviewer has five minutes for a PR — `watch` mode means a bug like the planted one gets flagged before it's even committed, when it's cheapest to fix. It also has a useful side effect: no verdict is possible without a clearly stated intent, which surfaces "I'm not actually sure what I just built" moments — a real, underrated failure mode when iterating fast with an AI assistant. In deployment, the pre-push hook and CI/PR integration block a risky change before it merges rather than after it ships, and the 3-way exit-code contract lets a CI pipeline make the correct automated call without a human in the loop for the common case — and correctly ask for one only when something's genuinely ambiguous or the checker itself is having a bad day.

**Bottom line:** this is a soundly built system that has demonstrably caught a real, subtle bug, repeatedly, under adversarial testing — not just once, not just on a happy path. That's a legitimate foundation for confidence. What it hasn't earned yet is field-proven trust on messy, large-scale real-world codebases — that's not a knock on the engineering, it's the honest next step the roadmap itself names. The right confidence to have right now: *this is worth betting on*, not *this is already proven at scale*.

## License

MIT — see [LICENSE](./LICENSE).
