# Manual test checklist — every aspect of the system

Organized by module/feature so nothing built so far is untested. Each item
is tagged **[P0]** (honesty guarantee — must never fail), **[P1]** (core
feature correctness), or **[P2]** (resilience/edge case).

Automated evidence for the gated phases lives in [`phase0/`](./phase0) and
[`phase1/gate_results.json`](./phase1/gate_results.json). This is the
human-run companion — it covers everything built after the Phase 1 gate
passed, plus a live sanity pass on the gated behavior itself.

Suggested test bed: a small throwaway repo with an intentional, subtle bug —
e.g. an in-memory login rate limiter that claims "5 attempts per account per
minute" but is actually keyed by IP address instead of account. No
network/DB dependency (so the sandbox runs it directly), and the bug is a
real intent-vs-behavior mismatch, not a syntax error.

## What's implemented (reference)

| Area | File | Status |
|---|---|---|
| Config & Setup | `verdict/config.py` | Phase 1 |
| Intent Extractor | `verdict/intent.py` | Phase 1 |
| Scenario Generator | `verdict/generator.py` | Phase 1 |
| Scenario Authoring (manual) | `verdict/authoring.py` | Phase 1 |
| Scenario Validator | `verdict/validator.py` | Phase 1 |
| Sandbox Runner | `verdict/sandbox.py` | Phase 1 + import-path fix |
| Risk Scorer | `verdict/scorer.py` | Phase 1 |
| Reporter | `verdict/reporter.py` | Phase 1 + `runs`/`report`/HTML |
| CLI + interactive shell | `verdict/cli.py` | Phase 1 + watch/path/confirm |
| Pluggable LLM providers | `verdict/llm.py`, `verdict/ollama.py` | post-Phase-1 |
| Live watch mode | `verdict/cli.py` (`watch`) | post-Phase-1 |
| Hybrid/manual merge | `verdict/hybrid.py` | post-Phase-1 |
| Git pre-push hook | `verdict/hooks.py` | post-Phase-1 |
| Audit log | `verdict/audit.py` | post-Phase-1 |
| Dead-function detection | `verdict/testgen.py` | correctness fix |
| Range vagueness fix | `verdict/intent.py` | correctness fix |
| Reproducibility fix (temp=0, seed=0) | `verdict/ollama.py`, `verdict/llm.py` | correctness fix |
| Validator embedded-term matching | `verdict/validator.py` | correctness fix |
| Intent display fix | `verdict/cli.py` | correctness fix |
| Broken-monkeypatch detection | `verdict/testgen.py` | correctness fix |
| FAILED-result confirmation | `verdict/cli.py` | correctness fix |
| One-shot provider setup (`init --provider/--api-key/--base-url`) | `verdict/cli.py` | correctness fix |
| Interactive `model` picker (live-fetched list, `/model` in shell) | `verdict/cli.py` | post-Phase-1 |
| User-Agent on outgoing cloud requests | `verdict/llm.py` | correctness fix |
| JSON-mode-rejection fallback | `verdict/llm.py`, `verdict/generator.py` | correctness fix |
| Rate-limit (429) Retry-After handling | `verdict/llm.py` | correctness fix |

## 1. Config & Setup

- [ ] **[P1]** `verdict init` with no flags → default `ollama` provider, correct model
- [ ] **[P1]** `verdict init --provider openrouter` (unknown provider) → rejected with valid-options message
- [ ] **[P0]** **Regression: one-shot cloud provider setup.** `verdict init --provider groq --model <id> --api-key <key>` in a single command writes provider+model+key together (no forced `config set` follow-up) and prints the cloud-privacy warning
- [ ] **[P0]** `verdict init --provider <cloud>` with no `--model` and no prior model for that provider → rejected with a provider-specific model-name hint, config NOT written
- [ ] **[P1]** Re-running `init` on the SAME provider (e.g. rotating `--api-key`) does NOT demand `--model` again — only an actual provider switch triggers the guard
- [ ] **[P1]** Switching provider back to `ollama` never requires `--model`
- [ ] **[P0]** `verdict model` (or `/model` in the shell): picking a cloud provider + entering its API key fetches the REAL model list from that provider's `/models` endpoint - never a hardcoded/guessed list
- [ ] **[P1]** `model` picker: typing a substring narrows the list (filter-to-one auto-selects); typing a number selects directly; blank keeps the current value at each step
- [ ] **[P0]** `model` picker: if live listing fails (bad key, provider down), falls back to manual model-id entry instead of crashing or silently guessing
- [ ] **[P1]** `model` picker: switching provider then blank-entering the API key with no existing key or env var → rejected, config unchanged
- [ ] **[P1]** In the interactive shell, a line starting with `/` (e.g. `/model`) behaves identically to the same word without the slash
- [ ] **[P0]** **Regression: API key prompt in `model` must accept pasted text.** The prompt is plain (unmasked) input, not `getpass`/`Prompt.ask(password=True)` - hidden-input raw-mode reading is known to silently drop or mangle clipboard-pasted text on Windows terminals, which fed a stale/invalid key straight into the provider call and surfaced as a confusing 403 instead of a clear "key not set" error
- [ ] **[P1]** Pasted key with stray surrounding quotes/whitespace (common when copying from a quoted source) is stripped before being saved
- [ ] **[P0]** **Regression: Groq requests must send a real User-Agent.** Groq's API sits behind Cloudflare, which blocks Python's default `Python-urllib/x.y` User-Agent as a bot (Cloudflare error 1010) - a VALID key still got a bare `HTTP 403: Forbidden` with zero auth-related detail, indistinguishable from an actual permission problem. Reproduced directly (curl with the default urllib UA → 403; same request with any normal UA → reaches Groq's real auth layer). Fixed by sending a `User-Agent: verdict-cli/0.1.0` header on every outgoing request in `llm.py` (both chat completions and the /models health check)
- [ ] **[P1]** Cloud-provider HTTP errors surface the response body (e.g. `{"error": ...}` or a WAF block page), not just the bare status code - needed to tell "bad key" apart from "blocked before it even reached the provider"
- [ ] **[P0]** **Regression: model that can't do enforced JSON mode must not hard-fail scenario-gen.** Some models (seen live: a Qwen build on Groq) return `HTTP 400 json_validate_failed` with an empty `failed_generation` when `response_format: json_object` is set - not a bad key or bad request, the model just can't produce valid output under forced JSON mode. `llm.call(..., json_format=True)` must detect this specific error and retry the SAME request once with JSON mode turned off before giving up, since the prompt already demands JSON-only text on its own
- [ ] **[P0]** An unrelated 400 (bad model id, bad request shape) must NOT be caught by the JSON-mode fallback - it must still raise immediately as before, no wasted retry
- [ ] **[P1]** Scenario-gen JSON parsing tolerates a markdown-fenced response (` ```json ... ``` `), not just bare JSON - needed once JSON mode isn't enforced and a model reverts to its default formatting habits
- [ ] **[P0]** **Regression: 429 (rate limit) must honor the provider's `Retry-After` header**, not just a fixed 1s/5s backoff - a free-tier requests/tokens-per-minute limit routinely needs far longer than that, so the fixed schedule burned all 3 attempts without ever waiting long enough
- [ ] **[P1]** A huge/malicious `Retry-After` value is capped (`RATE_LIMIT_MAX_WAIT` = 30s) so a single command can't hang indefinitely
- [ ] **[P1]** No `Retry-After` header present → falls back to the original fixed backoff schedule, unchanged
- [ ] **[P1]** Exhausting all attempts on repeated 429s raises a message that specifically names rate-limiting as the cause, not a generic "unreachable"
- [ ] **[P1]** `config get` (no key) → lists all keys, `api_key` masked as `****xxxx`
- [ ] **[P1]** `config set` for each key: `model`, `ollama_url`, `provider`, `api_key`, `base_url`
- [ ] **[P1]** `config set provider <invalid>` → rejected, config unchanged
- [ ] **[P0]** `.verdict/audit.jsonl` after any `config set api_key ...` → key appears masked, never raw
- [ ] **[P1]** `verdict health` → three independent checks (config/LLM/Docker), each can fail without affecting the others' reporting

## 2. Intent Extractor

- [ ] **[P1]** `extract_from_commit` — diff + message of a single ref
- [ ] **[P1]** `extract_from_range` (`--base`) — diff across a range, intent from combined commit subjects (all subjects present, not just HEAD's)
- [ ] **[P0]** **Regression: one vague commit must not poison a range with real history underneath.** If HEAD's message is a throwaway (`"wip"`) but an earlier commit in the same range is genuinely descriptive, the range must NOT be flagged vague — only flag a range vague if every commit subject in it is independently vague
- [ ] **[P1]** `extract_from_working_tree` (`--intent`) — uncommitted diff + explicit intent
- [ ] **[P0]** Vagueness detection catches: too-short message, placeholder patterns (`"wip"`, `"fix"`, `"final"`), low content-word count
- [ ] **[P0]** Vagueness detection does NOT false-positive on a real, descriptive intent
- [ ] **[P1]** `--path <file>` scopes the diff to exactly that file/folder across all three extraction modes

## 3a. Scenario Generator (the one bounded LLM step)

- [ ] **[P1]** Normal generation → valid scenario list parsed from JSON
- [ ] **[P1]** Malformed JSON on first attempt → retries (MAX_ATTEMPTS=2), succeeds or fails cleanly
- [ ] **[P0]** Refuses to run against a vague intent (raises, doesn't generate garbage scenarios)
- [ ] **[P0]** Refuses to run against an empty diff
- [ ] **[P1]** Token/timing counters populate correctly on the run record

## 3b. Scenario Authoring (manual)

- [ ] **[P1]** `plan --manual` writes an editable YAML template
- [ ] **[P0]** Running with unedited `example_` placeholder scenarios → refused, not silently accepted
- [ ] **[P1]** `load_scenarios` accepts both YAML and JSON

## 4. Scenario Validator

- [ ] **[P0]** A scenario that traces to real diff content is kept
- [ ] **[P0]** A hallucinated/generic scenario with no traceable overlap is dropped, and the drop reason is shown
- [ ] **[P0]** 0/N scenarios traceable → run finishes as **UNVERIFIED** (via `_finish_unverified`), never a silent error or a faked risk level

## 5. Sandbox Runner

- [ ] **[P0]** **Regression: repo modules are importable.** A generated test doing `from <repo module> import X` succeeds — this is the exact bug that was shipping silent `UNVERIFIED` results; check it explicitly every time, don't assume it stays fixed
- [ ] **[P0]** Exit 0 → `passed`; non-zero with a real assertion → `failed`; exit 2 → `uncertain`
- [ ] **[P0]** A broken check (`ImportError`/`AttributeError`/`NameError`/etc. as the last stderr line) → classified `error`, never counted as a failed change
- [ ] **[P2]** Memory-capped container (OOM, exit 137) → classified `error`, not `failed`
- [ ] **[P2]** Per-scenario timeout → container force-removed (`docker rm -f`), status `timeout`
- [ ] **[P2]** After a normal run, `docker ps -a` shows no leftover containers (`--rm` actually cleans up)
- [ ] **[P2]** `pip install` failure inside the container doesn't block test execution (best-effort install)

## 6. Risk Scorer

- [ ] **[P0]** 0 conclusive scenarios → `UNVERIFIED`
- [ ] **[P0]** conclusive evidence, 0 failures → `LOW`
- [ ] **[P1]** coverage ≥ 2/3 with ≥1 failure → `MEDIUM`
- [ ] **[P1]** coverage < 2/3 with ≥1 failure → `HIGH`
- [ ] **[P2]** boundary case at exactly 2/3 coverage lands on the documented side

## 7. Reporter

- [ ] **[P1]** `build_record` / `build_incomplete_record` — correct fields for completed vs errored/skipped runs
- [ ] **[P1]** `save_run` → `load_run` round-trips exactly
- [ ] **[P1]** `verdict runs` — table sorted newest-first, correct verdict/evidence columns for LOW/MEDIUM/HIGH/UNVERIFIED **and** errored/skipped rows
- [ ] **[P1]** `'last'` resolves to the newest run everywhere it's accepted (`logs`, `report`)
- [ ] **[P0]** `format_json` never leaks `diff`/`generation_prompt`/`test_code` (machine output stays slim, evidence stays in the full record on disk)
- [ ] **[P1]** `verdict report` — HTML renders correctly for a completed run AND for an errored/skipped one; opens standalone with no external assets

## 8. CLI + interactive shell

- [ ] **[P1]** Bare `verdict` → branded interactive shell; `help`, `clear`, `exit`/`quit` all work
- [ ] **[P1]** Every command runs identically inside the shell and as a direct CLI call
- [ ] **[P1]** `--json` on `run` produces valid, complete JSON
- [ ] **[P1]** `--path` works consistently across `run`, `plan`, and `watch`
- [ ] **[P0]** Exit code is 0 only when risk is `LOW`; every other outcome (MEDIUM/HIGH/UNVERIFIED/errored/skipped) exits non-zero

## 9. Live watch mode

- [ ] **[P1]** Several rapid edits within the settle window → exactly one verification fires, not one per save
- [ ] **[P1]** `.verdict/INTENT.md` is created on first run and read live on each cycle
- [ ] **[P0]** No intent available → warns and waits, never runs a guessed verdict
- [ ] **[P0]** Vague intent in `INTENT.md` → warns and waits for a better one
- [ ] **[P1]** Ctrl+C → clean exit with a session summary (count of verifications run)
- [ ] **[P1]** `--path` scoping limits both the watched fingerprint and the triggered run
- [ ] **[P0]** Verdict's own writes to `.verdict/` never re-trigger a verification (fingerprint excludes `.verdict/`)

## 10. Hybrid / manual merge

- [ ] **[P1]** `--scenarios <file>` alone → skips LLM scenario-gen entirely, runs only manual scenarios
- [ ] **[P1]** `--hybrid` → generated + manual scenarios merge
- [ ] **[P1]** Duplicate scenario (by name or ≥0.8 description overlap) → manual version wins, duplicate dropped
- [ ] **[P1]** `--hybrid` without `--scenarios` → rejected with a clear error

## 11. Git pre-push hook

- [ ] **[P1]** `install-hook` writes the hook; `uninstall-hook` removes it cleanly
- [ ] **[P0]** Installing over an existing foreign (non-Verdict) hook is refused, not overwritten
- [ ] **[P0]** Pushing a non-LOW-risk range is blocked
- [ ] **[P1]** `git push --no-verify` bypasses on purpose
- [ ] **[P0]** The hook verifies exactly the commit range being pushed, nothing more/less

## 12. Audit log

- [ ] **[P0]** Append-only: ids are strictly sequential, no entry is ever rewritten
- [ ] **[P1]** Unknown `action_type` is rejected at write time
- [ ] **[P0]** Every run's full lifecycle appears (`run_started` → `run_completed`/`run_errored`/`run_skipped`), none vanish
- [ ] **[P0]** `config_change` entries show masked keys only, before and after

## 13. Correctness fixes (this session — verify explicitly)

- [ ] **[P0]** **Dead-function detection**: a generated test that defines a check function but never calls it is caught by `find_dead_functions`, fed back to the model for a retry, and — if never fixed — the scenario becomes `ungeneratable` rather than a false `PASSED`
- [ ] **[P0]** **FAILED confirmation pass**: a scenario whose failure reproduces on independent regeneration stays `FAILED`; one that doesn't reproduce is downgraded to `uncertain` and recorded under `failure_not_reproduced` — never left as a confident but wrong `FAILED`
- [ ] **[P2]** Confirmation pass only fires for `FAILED` results — passed/uncertain/error scenarios don't pay the extra generation+sandbox cost
- [ ] **[P0]** **Broken-monkeypatch detection**: a generated test that does `from X import Y` and later `X.Y = fake` while also calling bare `Y(...)` is caught by `find_broken_monkeypatch` and fed back for a retry — this is exactly the failure shape the FAILED-confirmation pass structurally cannot catch (the model makes the same wrong assumption on every regeneration, so confirmation gives false confidence instead of catching it)
- [ ] **[P2]** `find_broken_monkeypatch` does NOT flag `unittest.mock.patch(...)` usage or consistent module-attribute access (only the direct-import + attribute-patch + bare-call combination)
- [ ] **[P0]** **Range vagueness fix**: a `--base` range whose HEAD commit is vague (`"wip"`) but contains a genuinely descriptive earlier commit is NOT flagged vague; a range where every commit is genuinely vague still is
- [ ] **[P0]** **Reproducibility fix**: the same fixed diff/intent, run through `verdict plan` repeatedly, produces the same scenario count and the same traceability result every time (temperature=0.0 + fixed seed) — a flip between e.g. 4/4 and 3/4 traceable on unchanged input is a regression
- [ ] **[P2]** A range built from a *moving* relative ref (e.g. `--base HEAD~2` re-run after adding more commits) is expected to change results — that's the ref pointing at different commits, not nondeterminism; don't confuse the two when testing
- [ ] **[P0]** **Validator embedded-term fix**: a scenario describing a real, embedded identifier form (e.g. `"password with digits"` against code that does `isdigit`) is traceable, not dropped — the term matcher must catch shared roots anywhere in the word, not just at the start
- [ ] **[P1]** **Intent display fix**: for a range accepted because an *older* commit is descriptive (not the newest), the displayed intent shows that governing commit, not just HEAD's message

## 14. Resilience / stress / isolation

- [ ] **[P2]** 10x in a row on the demo repo (live re-check of the Phase 1 gate shape) — zero crashes on current code
- [ ] **[P2]** Unicode/emoji in scenario output → no console crash on Windows
- [ ] **[P0]** Ollama down mid-run (killed during the scenario-gen call) → retry+backoff, then a clean `errored` record, no hang, no crash
- [ ] **[P0]** Docker down → clean `errored` record
- [ ] **[P2]** Ctrl+C mid-sandbox execution → no orphaned container afterward
- [ ] **[P2]** Cloud provider active with a bad/missing API key → clear error naming what's missing, not a crash
- [ ] **[P1]** Run from a completely separate repo → never touches this repo's own `.verdict/`, fully independent per-project state

## Recording results

Check items off directly in this file, noting the run id (`verdict runs`)
next to anything worth keeping as evidence:

```
- [x] Real bug caught (run_a1b2c3, HIGH, reason: "limiter keys by IP not account")
```

Any failed check is a correctness bug in the referee itself — file it
before moving on to Phase 2.
