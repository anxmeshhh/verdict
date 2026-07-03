# Manual test checklist — production readiness

This is the manual walkthrough for validating everything shipped so far
(Phase 1 + the post-Phase-1 additions: pluggable providers, `watch`, `--path`
scoping, human-readable history) against a real project, before trusting
Verdict on real work.

It's tiered by priority. **P0 must never fail** — those are the honesty
guarantees that are the entire point of the tool (silence beats a wrong
verdict; a broken check is never reported as a failed change; infra failure
is never dressed up as a real result). P1 is core feature coverage. P2 is
resilience under stress and unusual conditions.

Automated evidence for the gated phases lives in [`phase0/`](./phase0) and
[`phase1/gate_results.json`](./phase1/gate_results.json). This checklist is
the human-run companion to those — it exercises everything built *after*
the Phase 1 gate passed, plus a live sanity pass on the gated behavior itself.

Suggested test bed: a small throwaway repo with an intentional, subtle bug —
e.g. an in-memory login rate limiter that claims "5 attempts per account per
minute" but is actually keyed by IP address instead of account. Small enough
to read in one screen, has no network/DB dependency (so the sandbox can run
it directly), and the bug is a real intent-vs-behavior mismatch, not a syntax
error — exactly the class of thing Verdict exists to catch.

## P0 — Honesty guarantees

These prove Verdict never lies, even when things go wrong. If any of these
fail, nothing else on this list matters.

- [ ] **Real bug caught** — commit the IP-keyed rate limiter with an honest
      intent message → `verdict run` → risk is HIGH/MEDIUM, and the reported
      reason actually names the bug (not just "1 scenario failed")
- [ ] **Correct code passes** — fix the bug, commit → `verdict run` → LOW
- [ ] **Vague intent refused, not faked** — commit with message `"fix stuff"`
      → run status is `skipped`, never a guessed LOW/HIGH
- [ ] **Ollama down → errored, not crashed** — stop the Ollama server, run
      `verdict run` → clean "errored" output, exit code 1, no traceback
- [ ] **Docker down → errored** — stop Docker Desktop, run `verdict run` →
      same check
- [ ] **Empty diff → skipped** — run `verdict run` with no changes → skipped,
      not a fake LOW
- [ ] **Nothing vanishes** — after each case above, `verdict runs` and
      `verdict logs last` show every one of them recorded, including the
      errored/skipped runs

## P1 — Core features

- [ ] **Provider switch** — point `config set provider` at Groq/OpenRouter
      with a real key, full `verdict run` works end to end;
      `verdict config get api_key` shows a masked value;
      `.verdict/audit.jsonl` never contains the raw key
- [ ] **Bad/missing API key** — clear the key, run → a clear error naming
      what's missing, not a crash
- [ ] **`verdict watch` debounce** — start it, make several rapid edits
      within a couple seconds → exactly one verification fires after things
      go quiet, not one per keystroke
- [ ] **`watch` with no intent** — warns and waits, does not run garbage
- [ ] **Ctrl+C on `watch`** — clean exit with a session summary line
- [ ] **`--path` scoping** — scope a run to one file in a multi-file change
      → the diff and verdict concern only that file
- [ ] **Manual scenarios** — `verdict plan --manual`, edit the template,
      `verdict run --scenarios <file>` → skips LLM scenario-gen entirely
- [ ] **Hybrid mode** — same, with `--hybrid` → generated + manual scenarios
      merge, duplicates get dropped
- [ ] **`verdict report last`** — HTML opens and matches the terminal
      verdict exactly
- [ ] **Pre-push hook** — `verdict install-hook`, push a HIGH-risk commit →
      blocked; `git push --no-verify` bypasses on purpose

## P2 — Resilience and edge cases

- [ ] **10x in a row** on the demo repo (a live re-check of the Phase 1 gate
      shape) — zero crashes, on current code
- [ ] **Unicode output** — a scenario prints emoji/non-ASCII → no console
      crash on Windows
- [ ] **Kill Ollama mid-call** — stop the server during the scenario-gen LLM
      call → retry+backoff observed, then a clean errored record, no hang
- [ ] **Interrupt mid-sandbox** — Ctrl+C while a Docker container is
      executing → `docker ps -a` afterward shows no orphaned container
- [ ] **Cross-project isolation** — run from a completely separate repo →
      never touches this repo's own `.verdict/`, fully independent
      per-project state

## Recording results

Check items off directly in this file as they pass, and note the run id
(`verdict runs`) next to anything worth keeping as evidence, e.g.:

```
- [x] Real bug caught (run_a1b2c3, HIGH, reason: "limiter keys by IP not account")
```

If a check fails, it's a correctness bug in the referee itself — file it
before moving on to Phase 2.
