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

**Status (2026-07-03): Sections 2-12 worked through end-to-end, most via live
testing against a real demo repo (`Rate Limiter Test`) with that exact planted
bug. Section 1 parked by deliberate choice - see its own note below. Every
fix found along the way is documented in place, below, with what was
confirmed and how.**

**Status (2026-07-04): Phases 2-5 built in one pass, each phase gate-verified
before the next (Section 15). Gates: Phase 2 11/11, Phase 3 12/12, Phase 4
21/21 local (live PR check pending one repo secret), Phase 5 9/9 in 50.3s.
The cli.run() → pipeline.py refactor was proven byte-identical against a
pre-refactor snapshot before anything was built on top of it.**

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
| Reasoning-model `<think>` trace stripping | `verdict/generator.py`, `verdict/testgen.py` | correctness fix |
| Reproducibility pin (seed) on cloud providers | `verdict/llm.py` | correctness fix |
| Validator: unenforced-type-check hallucination guard | `verdict/validator.py` | correctness fix |
| Scenario-gen cache (`--force-regenerate`) | `verdict/generator.py`, `verdict/cli.py` | post-Phase-1 |
| Auto `.gitignore` for `.verdict/` on init/model | `verdict/config.py`, `verdict/cli.py` | correctness fix |
| Pre-push hook: remote-name + silent-skip fix | `verdict/hooks.py` | correctness fix |
| `--json` stdout purity (progress routed to stderr) | `verdict/ui.py`, `verdict/cli.py` | correctness fix |
| Silent `--max-scenarios` cap drop made visible | `verdict/cli.py`, `verdict/ui.py`, `verdict/reporter.py` | correctness fix |
| Inconclusive-scenario visibility in coverage displays | `verdict/ui.py`, `verdict/reporter.py` | correctness fix |
| Generalized unsupported-behavior-claim guard | `verdict/validator.py` | correctness fix |
| Postgres data layer + override/status/db | `verdict/store.py`, `verdict/cli.py` | Phase 2 |
| Shared pipeline (CLI/worker frontends) | `verdict/pipeline.py` | Phase 3 prep |
| check / 3-way exit codes / scope recap / profiles / scenario add | `verdict/cli.py`, `verdict/ui.py`, `verdict/reporter.py` | UX bundle |
| API gateway + Celery worker pool | `verdict/server/api.py`, `verdict/server/queue.py` | Phase 3 |
| Module 18 health + /metrics | `verdict/health.py` | Phase 3 |
| GitHub Action + webhook/Checks API | `action/`, `verdict/server/github.py` | Phase 4 |
| docker-compose + setup + DooD path map | `Dockerfile`, `docker-compose.yml`, `setup.*`, `verdict/sandbox.py` | Phase 5 |

## 1a. Scenario-gen cache (decided 2026-07-03: cache scenario-gen only, never testgen/execution)

- [ ] **[P0]** Same (diff, intent, model) re-run returns the identical previously-generated scenario set - only 1 real LLM call across N identical requests
- [ ] **[P0]** `--force-regenerate` bypasses the cache and makes a real LLM call every time, overwriting the cache entry with the fresh result
- [ ] **[P1]** A different model for the same diff+intent is a cache MISS (own cache entry, no cross-contamination)
- [ ] **[P0]** Bumping `CACHE_VERSION` (or any change to `PROMPT_TEMPLATE`, which changes the rendered prompt automatically) invalidates old cache entries - old scenario proposals never keep being served across a scenario-gen logic/prompt change
- [ ] **[P0]** A corrupt/unreadable cache file never breaks a run - falls back to a fresh LLM call silently
- [ ] **[P0]** Confirmed by design: the FAILED-confirmation pass in `cli.py` only calls `testgen.generate_test_code`, never `generator.generate` - scenario-gen caching cannot mask or interfere with confirmation-pass behavior
- [ ] **[P1]** `from_cache` is surfaced in the CLI stage line ("cached" note) and in the saved run record (`scenario_from_cache`) - a human or script reading a run can always tell whether scenario-gen actually called the LLM this run
- [ ] Deliberately NOT cached: validate, testgen, sandbox execution - a PASSED must mean the code was actually run this time; caching execution would let a verdict silently outlive a changed Docker image, model, or sandbox environment with no signal that anything changed
- [ ] Working-method note: repeat-run testing meant to surface intermittent model hallucinations (rerunning the same commit to see if a bad scenario recurs) now needs `--force-regenerate` explicitly, or every repeat is a guaranteed cache hit and looks artificially stable for the wrong reason

## 1. Config & Setup — parked by deliberate choice, not forgotten

Every other numbered section (2-12) has now been worked through end-to-end this
session, mostly via live testing against a real repo. Section 1 itself was never
formally re-walked item-by-item the same way - not because it's riskier or
lower-priority, but because config/setup got exercised constantly as a side
effect of everything else (every `init`, `model`, provider-switch, and
`config get/set` fix this session was itself a live test of this section).
Calling the checklist effort done at this point without a separate dedicated
pass here was an explicit choice, not an oversight - revisit if a real init/setup
bug ever surfaces, same as everything else in this file.

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
- [ ] **[P0]** **Regression: reasoning ("thinking") models must not surface as "model returned unusable JSON."** Groq's reasoning models (seen live: a Qwen build) prepend a `<think>...</think>` trace to the content by default outside enforced JSON mode - `json.loads` fails at char 0 on the leading `<`, indistinguishable at a glance from a genuinely empty response. `generator._extract_json` strips the think block, then trims to the outermost `{...}` unconditionally (safe no-op on already-clean JSON) to also drop any leading/trailing commentary. `testgen._strip_fences` gets the same think-block strip before its code-fence match
- [ ] **[P1]** A truly empty LLM response still raises the original clear JSON-decode error - the fallback recovers noise around real JSON, it does not manufacture scenarios or code from nothing
- [ ] **[P0]** **Regression: the temp=0/seed=0 reproducibility pin (originally ollama-only) must also apply to the cloud transport.** `_call_openai_compatible` now sends `"seed": 0` too - previously only `ollama.py` had it, so cloud providers were still fully unpinned despite `temperature=0`
- [ ] **[P0]** A provider that rejects `seed` as an unknown field (confirmed live: Gemini's OpenAI-compat layer - `Unknown name "seed": Cannot find field`) falls back to omitting it and still completes the request, rather than hard-failing every call to that provider
- [ ] **[P1]** Note for future runs: even with temp=0/seed=0, cloud providers do not guarantee bit-exact reproducibility (documented as "best effort" upstream) - this reduces variance, it does not eliminate it
- [ ] **[P0]** **Regression: validator must reject a scenario claiming Python enforces type hints at runtime.** ("`login(None, ...)` should raise TypeError" when the diff only has a type hint, no `isinstance`/`raise` check) - reproduced live on cdcf9dd on a cloud model; term-overlap traceability alone marked it traceable since "login"/"TypeError"-adjacent terms happened to overlap, even though the specific behavioral claim was invented. `_claims_unenforced_type_check` in validator.py catches this one pattern by checking the diff's ADDED lines for an explicit runtime check before allowing a TypeError/ValueError claim through
- [ ] **[P1]** A scenario making the same TypeError/ValueError claim where the diff DOES contain an explicit `isinstance()`/`raise` check must NOT be rejected by this guard - verified both directions
- [ ] **[P1]** Verified zero regressions against Phase 0's 100 labeled scenarios (none trigger the new rule - it's additive, not a replacement for term-overlap matching)
- [ ] Known scope, stated plainly: this catches one specific, real hallucination pattern (type-hint-implies-enforcement), the same way `find_dead_functions`/`find_broken_monkeypatch` catch specific patterns - it is NOT a general "does this behavioral claim logically follow from the diff" checker. That would require an LLM-judgment step, which is deliberately out of scope for a deterministic validator
- [ ] **[P0]** **Generalization: `_unsupported_behavior_claim` now covers 4 claim shapes, not just type-enforcement** - logging-on-failure, thread-safety, and format-validation added, all following the same claim-pattern → required-evidence-pattern structure. Still an explicitly named, growable list (same category as `find_dead_functions`), not a general semantic-entailment checker - a genuinely novel claim shape outside these 4 categories still passes through undetected, same as before
- [ ] **[P0]** **Regression caught before shipping via the Phase 0 dataset:** the first version of the logging-claim pattern (`\blogs?\b|\blogg(ed|ing)\b`) false-positived on "a user **logging in**" (auth, not audit logging) - `check_no_redirect_on_login` in Phase 0's labeled set, previously VALID, got wrongly rejected. Fixed by requiring the log-word NOT be immediately followed by in/out AND be near a fail/error/exception/warn/event/audit word. Re-verified 0 regressions against all 100 Phase 0 labeled scenarios after the fix
- [ ] **[P1]** Each of the 4 claim categories verified both directions: the unsupported claim (no evidence in diff) is rejected, and the identical claim WITH real supporting code (`isinstance`/`raise`, `logger.error(...)`, `with lock:`, `re.match(...)`) is NOT rejected
- [ ] **Standing decision (2026-07-03):** the systematic-mistake blind spot in FAILED-confirmation (a model's mistake that reproduces identically on regeneration can't be caught by confirm-and-retry) will be addressed by continuing to add narrow, named, deterministic static checks per newly-observed pattern (the `find_dead_functions` / `find_broken_monkeypatch` / `_unsupported_behavior_claim` family) - NOT by adding a second adversarial-review LLM call. Explicitly chosen over the second-model option to keep the "one narrow, bounded LLM step" architecture and avoid added cost/latency on every run. Revisit only if static checks visibly stop keeping up with new failure shapes
- [ ] **Process-lifetime caveat, worth remembering when re-testing a fix live:** the interactive `verdict` shell is one long-lived Python process - editing/committing source (even with an editable install) does NOT hot-reload already-imported modules in a shell that's already running. Always exit and start a fresh shell (or invoke `verdict` freshly from a plain terminal) after a code change before concluding a fix "didn't work" - confirmed this explains an apparent validator-fix regression that turned out to not exist (fresh-process test passed correctly; the reported reruns were ~1-2 minutes inside an already-running shell session)
- [ ] **[P0]** **Regression: coverage must not silently make inconclusive scenarios invisible.** `scorer.score()` deliberately excludes uncertain/error/timeout results from the coverage denominator (by design - non-evidence shouldn't count for or against a change), but the human-facing summary line ("3/3 conclusive passed · coverage 100%") said nothing about the excluded scenario - reading as "everything was checked" when one requirement produced zero evidence at all (in the reported case: a broken generated test that falsely claimed `check_login is not defined`, when it plainly is). Fixed by adding an explicit "N scenario(s) produced no evidence (excluded from coverage)" note to the headline in `ui.verdict_panel`, the compact `ui.runs_table` evidence column, `reporter.format_terminal`, and `reporter.render_html` - wherever the underlying scorer data was already correct but the summary display buried the caveat
- [ ] **[P1]** No spurious note when `inconclusive == 0` (a genuinely clean run) - verified against a real clean 4/4 run record
- [ ] **[P0]** **Regression caught before shipping:** the HTML report's added `&middot;` text was passed through `_esc()` along with the rest of `coverage_txt`, which would have double-escaped it into a literal visible `&amp;middot;` on the page. Fixed by using a plain "·" character instead of an HTML entity in that string. Verified the raw UTF-8 bytes in the rendered file are the actual middle-dot character, not the escaped entity text
- [ ] **[P1]** `config get` (no key) → lists all keys, `api_key` masked as `****xxxx`
- [ ] **[P1]** `config set` for each key: `model`, `ollama_url`, `provider`, `api_key`, `base_url`
- [ ] **[P1]** `config set provider <invalid>` → rejected, config unchanged
- [ ] **[P0]** `.verdict/audit.jsonl` after any `config set api_key ...` → key appears masked, never raw
- [ ] **[P0]** **Regression: `.verdict/` must be gitignored automatically, not left to the user to remember.** `.verdict/cache/`, `.verdict/runs/`, and `.verdict/audit.jsonl` hold full diffs, raw prompts, and raw LLM responses in plaintext - with no `.gitignore` entry, a plain `git add -A` stages all of it, silently defeating the exact leak `--json`'s clean output was designed to prevent. Confirmed live: no `.gitignore` existed in the real demo repo, and `.verdict/INTENT.md` (from earlier `watch`-mode testing) was ALREADY tracked in its git index before this fix - the leak isn't hypothetical, it already happened once (no remote on that repo, so nothing left the machine, but the mechanism is real)
- [ ] **[P0]** `verdict init` and `verdict model` both call `config.ensure_gitignore()` - creates `.gitignore` with a `.verdict/` entry if none exists, appends to an existing `.gitignore` if `.verdict/` isn't already covered, and is a no-op (returns None, file untouched) if it's already covered - verified all 3 cases plus idempotency (running twice doesn't double-append)
- [ ] Note: `.gitignore` only prevents FUTURE files from being staged - a file already tracked before the fix stays tracked until explicitly untracked (`git rm --cached`). This is a real, separate follow-up step for any repo where `.verdict/` was already committed before this fix shipped - not something `ensure_gitignore()` can retroactively fix on its own
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
- [x] **[P1]** **`scenario add` fixes (2026-07-04, found via a real `scenario add` + `run` test against the demo repo):**
  - Interactive name validation now happens the moment the name is typed (`validate_scenario_name`, shared with `append_scenario`'s own check) - an invalid name re-prompts immediately instead of accepting it, asking for the description too, then rejecting both.
  - `append_scenario` no longer writes a placeholder `intent: ''` key when no real intent is available - the key is omitted entirely rather than defaulting to an empty/stale value (caught because a real file had leaked `intent: tmp` - a stale value from earlier testing, not live code, but the empty-string default was the same class of problem and got fixed too).

## 3c. Testgen prompt hardening (2026-07-04)

- [x] **[P0]** **Dynamic-discovery argument-order bug, found via a real `run` against the demo repo's login/rate-limiter code.** When a generated test can't import the target function directly, its LLM-written fallback used to scan every `.py` file, match a callable by name (`login`/`authenticate`/`auth`/`sign_in`) and by `len(params) >= 2` alone, then call it *positionally* - producing `login(username, wrong_password, ip)` against the real `login(username, ip, password)`. This run still happened to land on the correct HIGH/FAILED verdict, but for the wrong reason (it exercised "same IP six times", not the scenario's actual "same account across different IPs"). The same mistake could just as easily produce a false PASS with different argument values - a live correctness risk, not cosmetic. Fixed at the prompt level (`testgen.py::PROMPT_TEMPLATE`): the model is now told to prefer a direct import of the exact function the diff names (almost always possible, since the diff headers show the file), and if it must fall back to scanning, to use `inspect.signature()` to match parameter *names* to the scenario's own wording and call with keyword arguments - never assume order from parameter count.

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
- [x] **[P1]** **Scenario-level concurrency (2026-07-04).** `run_all()` executes up to `--sandbox-concurrency` (default 3) containers at once via a bounded `ThreadPoolExecutor` — each scenario is a fully isolated container (own name, own scratch dir, repo mounted read-only), so no shared state to race on. The returned `results` list stays in original scenario order regardless of completion order (verified: a real run against the demo repo's `035175f` commit had `exceeds_limit_within_window` finish first live, but the saved record's `results` array was still `[same_user_different_ip, same_ip_different_user, exceeds_limit_within_window, window_reset_allows_login, old_attempts_pruned]` — the validate/testgen order). `on_result` fires in completion order for live progress. `max_workers<=1` or a single scenario takes the old plain-loop path unchanged (byte-identical to Phase 1 there). `--sandbox-concurrency 1` restores pure sequential behavior for anyone who wants it.

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
- [ ] **[P0]** **Regression: `--json` on `run` must produce PURE JSON on stdout, nothing else.** Confirmed live: progress/status lines (`✓ config`, `✓ intent`, `✓ scenario-gen`, ...) were printed via the same shared Rich console as the final JSON blob, both on stdout, both in the same stream a consumer (CI, `jq`, a dashboard) reads - proven directly by redirecting stderr away and taking the first line of stdout: it was `"+ config groq and Docker ready"`, not `{`. Fixed with `ui.route_to_stderr()`, called at the top of `run()` whenever `--json` is set, before anything can print - moves ALL progress output to stderr (the standard stdout=result/stderr=log convention), leaving only the final `typer.echo(format_json(...))` on stdout. A human watching a `--json` run in a terminal sees no difference (both streams interleave there by default)
- [ ] **[P1]** Verified both directions: with `--json`, stdout is exactly the JSON blob (parses cleanly, nothing before/after) and all progress lines land on stderr instead; without `--json`, behavior is completely unchanged (progress still goes to stdout as before) - no regression for interactive/human use
- [ ] **[P1]** `--path` works consistently across `run`, `plan`, and `watch`
- [ ] **[P0]** Exit code is 0 only when risk is `LOW`; every other outcome (MEDIUM/HIGH/UNVERIFIED/errored/skipped) exits non-zero
- [ ] **[P0]** **Regression: `--max-scenarios` silently dropped validated scenarios with zero signal.** Confirmed live via `--hybrid`: `validate` correctly reported "6/6 traceable to the diff," but `kept = kept[:max_scenarios]` (default 4) sliced the list AFTER that line printed, so 2 fully-validated scenarios never became a `GeneratedTest`, never ran, never appeared in `results`, `score()`, `format_json`, or the run record - completely invisible. Confirmed this wasn't a general cap bug: separate 4-scenario and 2-scenario runs executed all their scenarios fine - only the overflow past the cap vanished, silently. The real danger (per the report): if the cap had instead dropped one of the two scenarios that actually caught the planted bug, the run would report a confident, wrong verdict on reduced coverage while still claiming "6/6 traceable" - indistinguishable from a fully-verified run
- [ ] **[P0]** Fixed: the cap is still the same intentional cost/time control (LLM+sandbox spend per run) - not removed - but now fully explicit everywhere: `ui.stage_warn` at truncation time naming exactly which scenarios were NOT run; `record["scenario_cap_dropped"]` persisted in the run record/JSON; a headline note in `ui.verdict_panel`, `reporter.format_terminal`, `reporter.render_html`, and `ui.runs_table`'s evidence column - the same "silent is the problem, visible is fine" fix applied to `--json`/`.gitignore`/the pre-push hook earlier this session
- [ ] **[P1]** Verified all 4 display surfaces show the cap-dropped names when present, and show nothing extra on a clean, uncapped run (no regression to normal display)
- [ ] **Follow-up decision (2026-07-03):** `--max-scenarios` default raised from 4 to 8 on `run`. 4 was too easy to hit even in ordinary `--hybrid` use: autonomous scenario-gen's own prompt asks for 2-5, and `--hybrid` adds manual scenarios on top of that (the reported case was 2 manual + 4 generated = 6, already past the old default). 8 gives real headroom for the common combined case while staying a deliberate, meaningful cost bound - not "no cap." `verdict watch`'s own separate default (3, tuned for frequent live-triggering cost) is intentionally untouched - different tradeoff, not part of this decision

## 9. Live watch mode — SECTION COMPLETE

- [x] **[P1]** Several rapid edits within the settle window → exactly one verification fires, not one per save
- [x] **[P1]** `.verdict/INTENT.md` is created on first run and read live on each cycle
- [x] **[P0]** No intent available → warns and waits, never runs a guessed verdict
- [x] **[P0]** Vague intent in `INTENT.md` → warns and waits for a better one
- [x] **[P1]** Ctrl+C → clean exit with a session summary (count of verifications run)
- [x] **[P1]** `--path` scoping limits both the watched fingerprint and the triggered run
- [x] **[P0]** Verdict's own writes to `.verdict/` never re-trigger a verification (fingerprint excludes `.verdict/`)
- [x] **Design question, confirmed intentional (not a bug):** every settle-triggered verification re-verifies the FULL uncommitted diff (`git diff HEAD` via `extract_from_working_tree`), not an incremental slice since the previous trigger. Confirmed directly in code - `watch()` always calls `run(ref=None, base=None, intent=...)`, which routes to `extract_from_working_tree`, which diffs against `HEAD` every time, unconditionally. This is deliberate: it matches how the rest of Verdict verifies (full range from a base to current state, same semantics as the pre-push hook), so watch's output always answers "if I committed/pushed everything right now, what would happen" - not the narrower and less useful "did just my last edit break something in isolation" (an incremental-only check could look clean while the cumulative diff has a real issue spanning two edits)
- [ ] **Known cost tradeoff, not fixed - flagging, not deciding unilaterally:** because the diff grows every trigger, the exact prompt text scenario-gen caching is keyed on also changes every trigger - meaning the scenario-gen cache effectively never hits during an active watch session (every settled state has a textually different diff than the last). In a long session against a cloud provider, this means every settle-fire pays a full fresh LLM+sandbox cost, which could get expensive as accumulated uncommitted changes grow in a larger repo. Worth a deliberate decision later (e.g. periodic re-basing of "what's already been verified," or accepting the cost as the price of full-range accuracy) - not changed here

## 10. Hybrid / manual merge

- [ ] **[P1]** `--scenarios <file>` alone → skips LLM scenario-gen entirely, runs only manual scenarios
- [ ] **[P1]** `--hybrid` → generated + manual scenarios merge
- [ ] **[P1]** Duplicate scenario (by name or ≥0.8 description overlap) → manual version wins, duplicate dropped
- [ ] **[P1]** `--hybrid` without `--scenarios` → rejected with a clear error

## 11. Git pre-push hook — SECTION COMPLETE (verified twice: sandbox + live real-repo testing)

- [x] **[P1]** `install-hook` writes the hook; `uninstall-hook` removes it cleanly - live-confirmed the hook FILE is actually deleted (not just disabled/neutered), and a push afterward behaves exactly like an unhooked repo
- [x] **[P0]** Installing over an existing foreign (non-Verdict) hook is refused, not overwritten - confirmed both in sandbox and live
- [x] **[P0]** Pushing a non-LOW-risk range is blocked - confirmed live for real: a genuinely HIGH-risk push was blocked, exit code caused git to refuse it (not a simulated/mocked result)
- [x] **[P1]** `git push --no-verify` bypasses on purpose - confirmed live, clean
- [x] **[P0]** The hook verifies exactly the commit range being pushed, nothing more/less
- [x] **[P0]** **Regression: the hook must not silently skip verification on a new-branch push.** Root cause confirmed live with a real bare local remote added under a non-"origin" name: `remote_sha=zero` (first push of a new branch - one of the most common real git workflows, not an edge case) fell back to `git merge-base "$local_sha" origin/HEAD`, which doesn't exist unless the remote happens to be named `origin` AND has `HEAD` tracked locally - neither is guaranteed. The old script's fallback for "no merge-base found" was a bare `continue`: zero output, exit 0, a push that was never checked looks byte-identical to one that passed
- [x] **[P0]** Fixed: use `$1` (the remote name git actually passes to a pre-push hook) instead of hardcoding `origin`, and try the remote's tracked `HEAD` first, then `main`/`master` as fallback candidate branch names, before giving up
- [x] **[P0]** If truly no base can be determined (genuinely orphaned first branch, remote has nothing to compare against), the hook now prints an explicit "could not determine a base commit - skipping verification (nothing was checked)" line instead of silently exiting - verified this path is reachable and non-silent
- [x] **[P0]** Verified end-to-end against a real bare-repo sandbox mirroring the exact reported setup (non-"origin" remote name, no `HEAD` ref tracked, only `main` present): old hook script silently skips with zero output; new hook script correctly resolves the base via the remote's `main` and reaches `verdict run --base <resolved> --ref <local>` with the right arguments
- [x] **[P1]** `install-hook` auto-upgrades an outdated verdict-installed hook in place (detected via marker present + content differs) instead of requiring a manual uninstall/reinstall round-trip - verified: outdated hook gets replaced, a second install() call correctly reports "already installed and up to date," and a genuinely foreign (non-verdict) hook is still refused, not clobbered; independently re-confirmed live (the fixed version correctly replaced the buggy one in place on the real test repo)

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

## 15. Phases 2-5 (built 2026-07-04 in one pass; each phase gate-verified before the next)

Gate evidence lives beside each phase: `phase2/gate_results.json` (11/11),
`phase3/gate_results.json` (12/12), `phase4/test_results.json` (21/21 local),
`phase5/gate_results.json` (9/9, 50.3s vs the 600s budget). Items below are
the standing regression surface on top of those gates.

### 15a. Data layer (`verdict/store.py`) + override

- [x] **[P0]** Dual-write: one `save_run()` call lands in the file AND Postgres when `database_url` is set; file store stays canonical and byte-identical with no DB configured (regression-swept live on the demo repo)
- [x] **[P0]** "Why did it flag this" answerable from SQL alone for a REAL HIGH-risk run: reasons, per-scenario stdout evidence + test code, exact prompt + raw response, audit lifecycle
- [x] **[P0]** `override` requires `--reason`, is INSERT-only (annotates, never edits), audit-logged, and prints the running override rate (the Section 13 first-class metric); OVERRIDDEN surfaces in runs/logs/report/HTML
- [x] **[P1]** `db migrate-files` backfills the whole file history idempotently (66 real runs + 143 audit entries in the gate)
- [x] **[P0]** DB unreachable in CLI mode → one loud stderr warning, file write still succeeds; `init`/`model` no longer wipe `database_url` when rebuilding Config (bug found and fixed during the pipeline refactor)

### 15b. Pipeline refactor (`verdict/pipeline.py`)

- [x] **[P0]** **Equivalence proven, not assumed:** deterministic mocked-LLM harness captured rich stdout/stderr, `--json` stdout/stderr, the saved record, and exit codes BEFORE the refactor and AFTER — byte-for-byte identical diff across all artifacts; abort path (invalid ref → errored record + exit) verified separately
- [x] **[P1]** The default `PipelineEvents` (all no-ops) produces identical records/audit entries — any frontend gets the same verdict

### 15c. UX bundle

- [x] **[P0]** 3-way exit codes on `run`/`check`: 0=LOW, 1=risky verdict, 2=verdict couldn't verify — CI can tell "block the merge" from "the checker is broken"; the pre-push hook still blocks on ANY non-zero (can't-verify never silently passes)
- [x] **[P0]** `verdict check`: uncommitted changes → working tree (INTENT.md; clear exit-2 if missing/vague); clean tree → last commit; inferred scope printed before anything runs
- [x] **[P1]** Scope recap ("checked: single commit X / commit range A..B / uncommitted working tree") stored in every record incl. errored ones, shown in panel/logs/terminal/HTML/github formats; old records without the field render fine (regression-swept)
- [x] **[P1]** "N/M planned scenario(s) executed" always visible — executed vs planned can never silently diverge (highlighted when they disagree)
- [x] **[P0]** `verdict profile save/list/delete` + `verdict use <name>`: secrets typed once, switched by name, masked in every display, switches audit-logged
- [x] **[P1]** `verdict scenario add/list`: interactive authoring, duplicate names rejected, template placeholders auto-dropped, output loads through the same Module 3b path
- [x] **[P0]** `--json` emits exactly one JSON object on stdout for EVERY outcome including errored/skipped (previously an aborted --json run printed nothing — never-silent violation, fixed during Phase 4)

### 15d. Server mode (API + queue + Module 18)

- [x] **[P0]** 5 concurrent runs through the real stack (uvicorn + Celery + Redis + Postgres + Docker): all complete, distinct run ids, exactly 2 result rows per run — no drops, no double-runs (11.6s wall)
- [x] **[P0]** Dedupe by UNIQUE constraint on (repo, base, head SHA, model, prompt-contract version); `force=true` deliberately bypasses; a job whose enqueue fails is rolled back so it can't dedupe-block forever
- [x] **[P0]** Honest degradation, proven live: Redis stopped mid-gate → `/health` 503 with the component named, new work refused outright, clean recovery on restart; LLM down → job parks as `waiting_on_llm` and retries, never dropped
- [x] **[P1]** Disk health has two thresholds (WARN 85% — loud but operational, per the doc's own example; CRITICAL 95% — unhealthy): the first gate run itself caught this distinction on a real 87%-full disk
- [x] **[P1]** `/metrics` Prometheus text (`verdict_health_status{component=...}`, `verdict_queue_depth`); `/health` + `/metrics` never require auth — an unhealthy system must be observable while unhealthy

### 15e. GitHub integration

- [x] **[P0]** Webhook: timing-safe HMAC verification; unsigned/tampered payloads rejected; webhooks disabled outright with no secret configured (a forged payload must never trigger clones or runs)
- [x] **[P0]** `prepare_repo` force-checkouts the exact PR head SHA — the sandbox mounts the working tree, so the working tree must BE the commit under verification (verified both directions)
- [x] **[P0]** Exit-code → check conclusion: LOW→success, risky→failure, could-not-verify→NEUTRAL (checker broke ≠ code risky), and the Action's gate step still fails on 2 — can't-verify never silently passes a PR
- [x] **[P1]** `format_github`: scope up front, executed-vs-planned, per-scenario evidence table, cap-drops + overrides called out; errored runs phrased explicitly as a checker problem, not evidence about the code
- [ ] **[P0]** LIVE gate: a real PR gets an accurate check unaided — needs one user step (add a `VERDICT_API_KEY` repo secret; see `phase4/README.md`), then open a PR with a planted intent-vs-behavior bug and a clean PR

### 15f. Packaging

- [x] **[P0]** Stranger-clone gate ran literally: fresh `git clone` (committed files only) → `.env` as setup.sh writes it → `compose up --build` → authenticated POST /runs → LOW verdict through api→redis→worker→DooD sandbox — 50.3s total
- [x] **[P0]** DooD path mapping: sandbox `-v` mounts translated container→host via `VERDICT_HOST_PATH_MAP`; per-test scratch moved out of container-local temp (which the host daemon can't see — the test file would silently vanish) into the shared data volume; both no-ops for the plain CLI
- [x] **[P1]** `.gitattributes` pins LF on shell scripts (a Windows clone would otherwise break the Linux entrypoint); `.env.example` un-ignored (secretless template must ship); API key enforcement verified from the fresh clone (401 without header)

## 16. v2 testing round (2026-07-04) - 7 tasks, 2 real bugs found and fixed

- [x] **[P0]** **`verdict check` false-positive on untracked-only changes.** The dirty-check used `git status --porcelain`, which flags untracked (`??`) files, but the actual diff (`extract_from_working_tree`) uses `git diff HEAD`, which never shows untracked files - so a stray untracked file (e.g. an untracked `.gitignore`) made `check` think there was something to verify, then handed the pipeline an empty diff instead of falling back to "clean tree -> last commit". Fixed by making the dirty-check use the exact same `git diff HEAD` command that will actually produce the diff, so the two can never disagree. Verified both directions: untracked-only -> correctly falls back to last commit; a real tracked edit -> still correctly detected as dirty.
- [x] **[P0]** **Testgen's provider-error/model-failure conflation.** `GenerationError` gained a `provider_error: bool` field, set when the failure came from `llm.LLMDown` (rate-limited/unreachable provider) rather than the model producing unsound code after retries. `pipeline.py`'s testgen loop now escalates to `errored`/exit-2 ("the LLM provider failed on every scenario") only when EVERY failed scenario was a provider error - a mixed or all-genuine-quality failure still correctly stays `unverified`/exit-1. Verified with both boundary cases via a monkeypatched `generate_test_code` (no real API calls): all-provider-error -> `errored`; single genuine failure -> `unverified`, not over-escalated.
- [x] **[P1]** Re-verified (already fixed earlier this session): scenario-add argument-order guessing, name-validation timing, stale intent placeholder - see Section 3c and 3b.
- [x] **[P1]** Confirmed working well: `check`'s uncommitted->last-commit priority (once the untracked-file bug is fixed), profile switching with no secret retyping, scenario add's interactive flow, the 0/1/2 exit code contract, and scenario-level concurrency (order-preserving under real load, see Section 5).

Design questions raised, not bugs - recorded for later, not yet decided:
- Should `override` work in plain file-store mode (via `audit.jsonl`) instead of requiring Postgres just to record one override?
- Does `watch` mode's full-history-every-cycle diff scope need narrowing for larger repos? (carried over from earlier)

## Recording results

Check items off directly in this file, noting the run id (`verdict runs`)
next to anything worth keeping as evidence:

```
- [x] Real bug caught (run_a1b2c3, HIGH, reason: "limiter keys by IP not account")
```

Any failed check is a correctness bug in the referee itself — file it
before moving on.
