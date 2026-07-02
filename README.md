# Verdict

An independent, self-hosted pre-deployment verification system for AI-generated and human-written code.

Verdict reads a code diff plus its stated intent, and proves — through generated, executed tests — whether the change actually does what it claims, before a human reviews it. It is not another AI coding assistant; it is the neutral referee that sits downstream of tools like Cursor, Claude Code, and Antigravity, verifying their output rather than producing more of it.

Full original vision doc (architecture rationale, worked examples, design discussion): [`Verifier_Project_Direction.docx`](./Verifier_Project_Direction.docx). This README tracks current, living status — the doc predates the project's rename from "Verifier" to "Verdict" and won't reflect progress made after it was written.

Not agentic, by design: Verdict is a **deterministic pipeline with exactly one bounded LLM step** (scenario generation). Every other stage — sandbox execution, risk scoring, reporting — is pure deterministic logic. Autonomous agent loops are unpredictable and hard to audit, a bad fit for a tool whose entire value is trustworthiness.

## Status

**Phase 0: PASSED.** 85% precision (gate was >70%) — see [`phase0/`](./phase0) for the script, dataset, and evidence. 25 real `{diff, intent}` pairs pulled from actual commit history, scenarios generated via local `qwen2.5-coder:7b`, manually judged for traceability against the real diff content.

Key finding from Phase 0: scenario quality was excellent (mostly 100%) on commits with clear, descriptive intent, and degraded badly on vague/placeholder commit messages (e.g. "fix in the rapidapi or ytdlp"). This means **Module 2 (Intent Extractor) needs real vagueness detection**, not just pass-through — a design requirement discovered empirically, not assumed upfront.

**Now building: Phase 1** — core pipeline as a CLI tool, single run, no queue/DB/UI. Gate: runs cleanly end-to-end on a real repo, 10 times in a row, no crashes. Building module-by-module, each one fully working before starting the next (not scaffolding everything up front).

Docker is available on the dev machine for Module 5 (Sandbox Runner).

## Phased roadmap

| Phase | Deliverable | Gate to proceed |
|---|---|---|
| 0 | ✅ Offline precision validation, no infra | Precision > 70% |
| 1 | 🔨 Core pipeline as a CLI tool | Runs cleanly end-to-end, 10x in a row, no crashes |
| 2 | Control & trust layer — config, override, logs, Postgres | Can answer "why did it flag this" from stored data alone |
| 3 | Concurrency & reliability — Redis queue, worker pool, health checks | 5 concurrent runs, no dropped/duplicated jobs |
| 4 | GitHub integration — webhook, Checks API, Action wrapper | A real PR gets an accurate check, unaided |
| 5 | Packaging — docker-compose, setup script | A stranger clones it and gets a working run in <10 min |
| 6 | Public validation — publish precision/recall numbers | Real external users, real override data |
| 7 | Dashboard — read-only web UI | Preferred over raw logs for daily use |
| 8 | Real-time layer — WebSocket, settlement-based live triggering | Live mode <5s/save, doesn't compete with an active agent |
| 9 | IDE extension — inline decorations | Matches dashboard/CLI exactly, no drift |
| 10 | Scale hardening — Firecracker, vLLM, fine-tuning | Only if metrics prove a bottleneck |

Phase 1 is already a real, usable tool even if the project stopped there. Phase 5 is the point a stranger can install and trust it. Everything past Phase 6 is additive reach.

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
| LLM runtime | Ollama (v1) → vLLM (scale-out later) |
| Model | `qwen2.5-coder:7b` |
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

## Local model setup

Verdict's scenario generation step runs against a self-hosted [Ollama](https://ollama.com) instance — no cloud LLM API is used, by design.

- Model: `qwen2.5-coder:7b`
- Run `ollama serve` to start the local inference server before running Verdict.

## License

MIT — see [LICENSE](./LICENSE).
