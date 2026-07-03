# Verdict

An independent, self-hosted pre-deployment verification system for AI-generated and human-written code.

Verdict reads a code diff plus its stated intent, and proves — through generated, executed tests — whether the change actually does what it claims, before a human reviews it. It is not another AI coding assistant; it is the neutral referee that sits downstream of tools like Cursor, Claude Code, and Antigravity, verifying their output rather than producing more of it.

Full original vision doc (architecture rationale, worked examples, design discussion): [`Verifier_Project_Direction.docx`](./Verifier_Project_Direction.docx). This README tracks current, living status — the doc predates the project's rename from "Verifier" to "Verdict" and won't reflect progress made after it was written.

Not agentic, by design: Verdict is a **deterministic pipeline with exactly one bounded LLM step** (scenario generation). Every other stage — sandbox execution, risk scoring, reporting — is pure deterministic logic. Autonomous agent loops are unpredictable and hard to audit, a bad fit for a tool whose entire value is trustworthiness.

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

**Recorded for later (deliberately not built yet):** a `verdict.yaml` project-runner declaration (services/test-command/seed data — what full-stack repos need the sandbox to stand up), diff chunking for very large PRs, per-service sandbox secrets, and scenario-level (within-run) sandbox concurrency.

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

```
verdict                          # branded interactive shell
verdict check                    # verify the obvious thing - no flags (uncommitted changes, else last commit)
verdict run                      # verify HEAD (or --ref, --base, --intent for working tree)
verdict run --path src/auth/     # verify only the files/folders you choose (repeatable)
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

## License

MIT — see [LICENSE](./LICENSE).
