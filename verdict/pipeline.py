"""
The pipeline orchestration - intent -> scenarios -> validate -> sandbox ->
score -> record - extracted from the CLI so every frontend drives the exact
same code: the CLI renders it with Rich, the Phase 3 worker records stage
transitions to Postgres, tests drive it headless.

Deliberately NOT a framework: one function, one events interface, control
flow identical to the Phase 1 CLI it was extracted from. The events object
is presentation only - nothing in here depends on what a frontend does with
an event, and a frontend that does nothing (the default PipelineEvents)
still produces the same records, audit entries, and outcome.
"""
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

from verdict import audit, llm
from verdict.authoring import AuthoringError, load_scenarios
from verdict.config import Config
from verdict.generator import GenerationError, generate
from verdict.hybrid import merge
from verdict.intent import (
    GitError,
    IntentResult,
    check_vagueness,
    extract_from_commit,
    extract_from_range,
    extract_from_working_tree,
)
from verdict.reporter import build_incomplete_record, build_record, new_run_id, save_run
from verdict.sandbox import DEFAULT_SANDBOX_CONCURRENCY, SandboxError, check_docker, run_all, run_test
from verdict.scorer import score
from verdict.testgen import generate_test_code
from verdict.validator import validate


@dataclass
class PipelineParams:
    ref: str | None = None
    base: str | None = None
    intent: str | None = None
    paths: list[str] | None = None
    scenarios_file: Path | None = None
    hybrid: bool = False
    max_scenarios: int = 8
    timeout: int = 300
    force_regenerate: bool = False
    sandbox_concurrency: int = DEFAULT_SANDBOX_CONCURRENCY

    @property
    def mode(self) -> str:
        return "hybrid" if self.hybrid else ("manual" if self.scenarios_file else "autonomous")

    @property
    def scope(self) -> str:
        """One line saying exactly what is being verified - shown up front on
        every run and stored in the record, so nobody has to reconstruct
        'what did this actually check' from scattered output."""
        if self.base:
            desc = f"commit range {self.base}..{self.ref or 'HEAD'}"
        elif self.ref:
            desc = f"single commit {self.ref}"
        elif self.intent is not None:
            desc = "uncommitted working tree vs HEAD"
        else:
            desc = "last commit (HEAD)"
        if self.paths:
            desc += f", limited to {', '.join(self.paths)}"
        return desc


class PipelineEvents:
    """Presentation hooks. Every method is a no-op by default so a headless
    caller (worker, tests) needs nothing; the CLI overrides them to
    reproduce its exact Phase 1 output."""

    def stage_ok(self, name: str, detail: str = "") -> None: ...
    def stage_warn(self, name: str, detail: str) -> None: ...
    def stage_fail(self, name: str, detail: str) -> None: ...
    def stage_note(self, name: str, detail: str) -> None: ...
    def scenario_line(self, name: str) -> None: ...
    def dropped_scenario(self, name: str, reason: str, cross_glyph: bool) -> None: ...
    def result_line(self, scenario_name: str, status: str, duration_s: float) -> None: ...
    def recorded_incomplete(self, status: str, run_id: str) -> None: ...

    def working(self, message: str):
        return nullcontext()


@dataclass
class PipelineOutcome:
    status: str  # "completed" | "unverified" | "errored" | "skipped"
    record: dict
    run_id: str

    @property
    def risk_level(self) -> str | None:
        return (self.record.get("risk") or {}).get("level")


class _Abort(Exception):
    def __init__(self, stage: str, message: str, status: str = "errored"):
        self.stage, self.message, self.status = stage, message, status


class _Unverified(Exception):
    def __init__(self, stage: str, note: str, generation):
        self.stage, self.note, self.generation = stage, note, generation


def extract_intent(
    repo: Path,
    ref: str | None,
    base: str | None,
    intent: str | None,
    paths: list[str] | None = None,
) -> IntentResult:
    if base:
        return extract_from_range(repo, base, ref or "HEAD", intent=intent, paths=paths)
    if ref:
        return extract_from_commit(repo, ref, paths=paths)
    if intent is not None:
        return extract_from_working_tree(repo, intent, paths=paths)
    return extract_from_commit(repo, "HEAD", paths=paths)


def display_intent_line(intent: str) -> str:
    """A range's intent can be several commit subjects joined together; the
    one that actually cleared the vagueness bar (and is doing the real work
    of describing the change) isn't necessarily the newest commit. Show that
    one instead of blindly always showing line 0 - a human skimming
    intent: "tmp" when the run was accepted has every reason to wonder what
    actually justified it."""
    lines = [ln for ln in intent.splitlines() if ln.strip()]
    if not lines:
        return ""
    for line in lines:
        if check_vagueness(line) is None:
            extra = f"  [dim](+{len(lines) - 1} more commit(s) in range)[/]" if len(lines) > 1 else ""
            return line[:70] + extra
    return lines[0][:70]


def execute_pipeline(
    params: PipelineParams,
    config: Config,
    repo: Path,
    events: PipelineEvents | None = None,
    run_id: str | None = None,
) -> PipelineOutcome:
    """The full pipeline. Always leaves a saved record and audit entries -
    completed, unverified, errored, or skipped - and never raises for
    verdict-level outcomes; the returned status says what happened."""
    events = events or PipelineEvents()
    run_id = run_id or new_run_id()
    tokens = {"llm_calls": 0, "prompt_tokens": 0, "output_tokens": 0, "llm_seconds": 0.0}
    holder = _IntentHolder()

    def _track(prompt_tokens: int, output_tokens: int, seconds: float) -> None:
        tokens["llm_calls"] += 1
        tokens["prompt_tokens"] += prompt_tokens
        tokens["output_tokens"] += output_tokens
        tokens["llm_seconds"] = round(tokens["llm_seconds"] + seconds, 2)

    try:
        return _execute(params, config, repo, events, run_id, tokens, _track, holder)
    except _Abort as a:
        # A run that dies still leaves a record and an audit entry.
        record = build_incomplete_record(
            run_id, a.status, a.stage, a.message, llm.model_id(config), holder.value, tokens
        )
        record["scope"] = params.scope
        save_run(record, repo)
        audit.append(f"run_{a.status}", {"stage": a.stage, "reason": a.message}, run_id=run_id, root=repo)
        events.stage_fail(a.stage, a.message)
        events.recorded_incomplete(a.status, run_id)
        return PipelineOutcome(status=a.status, record=record, run_id=run_id)
    except _Unverified as u:
        # The pipeline completed but produced zero conclusive evidence.
        # That is a verdict (UNVERIFIED), not an infrastructure error.
        risk = score([])
        risk.reasons.insert(0, f"{u.stage}: {u.note}")
        events.stage_ok("score", risk.level)
        record = build_record(run_id, holder.value, u.generation, [], risk, llm.model_id(config), tokens)
        record["note"] = u.note
        record["scope"] = params.scope
        save_run(record, repo)
        audit.append(
            "run_completed",
            {"risk": risk.level, "passed": 0, "failed": 0, "inconclusive": 0, "note": u.note, "tokens": tokens},
            run_id=run_id,
            root=repo,
        )
        return PipelineOutcome(status="unverified", record=record, run_id=run_id)


class _IntentHolder:
    """The abort/unverified handlers need whatever intent_result existed at
    failure time - a tiny mutable cell keeps the main flow readable."""
    value: IntentResult | None = None


def _execute(
    params: PipelineParams,
    config: Config,
    repo: Path,
    events: PipelineEvents,
    run_id: str,
    tokens: dict,
    _track,
    holder: "_IntentHolder",
) -> PipelineOutcome:
    audit.append(
        "run_started",
        {
            "mode": params.mode,
            "ref": params.ref,
            "base": params.base,
            "explicit_intent": bool(params.intent),
            "paths": list(params.paths or []),
        },
        run_id=run_id,
        root=repo,
    )
    events.stage_note("scope", params.scope)

    # [1/6] config
    with events.working("checking dependencies..."):
        status = llm.check(config)
        docker_ok = check_docker()
    needs_llm = params.scenarios_file is None
    if needs_llm and not status.reachable:
        raise _Abort("config", f"{config.provider} not reachable: {status.error or 'no response'}")
    if needs_llm and llm.is_local(config) and status.model_known is False:
        raise _Abort("config", f"model '{config.model}' not pulled")
    if not docker_ok:
        raise _Abort("config", "Docker daemon not reachable")
    events.stage_ok("config", f"{config.provider} and Docker ready")

    # [2/6] intent
    try:
        intent_result = extract_intent(repo, params.ref, params.base, params.intent, paths=params.paths)
        holder.value = intent_result
    except GitError as e:
        raise _Abort("intent", str(e))
    if not intent_result.diff.strip():
        scope = f" under {', '.join(params.paths)}" if params.paths else ""
        raise _Abort("intent", f"diff is empty{scope} - nothing to verify", status="skipped")
    events.stage_ok("intent", f'"{display_intent_line(intent_result.intent)}"')
    if params.paths:
        events.stage_note("scope", f"only verifying: {', '.join(params.paths)}")

    # [3/6] scenarios (generate, load, or hybrid-merge)
    if params.scenarios_file and params.hybrid:
        try:
            manual_gen = load_scenarios(params.scenarios_file)
        except AuthoringError as e:
            raise _Abort("scenario-load", str(e))
        events.stage_ok("scenario-load", f"{len(manual_gen.scenarios)} manual scenario(s) from {params.scenarios_file.name}")
        if intent_result.vague:
            events.stage_warn("scenario-gen", f"intent too vague to generate ({intent_result.vague_reason}) - manual only")
            generation = manual_gen
        else:
            try:
                with events.working(f"asking {config.model} for scenarios..."):
                    llm_gen = generate(intent_result, config, repo=repo, force=params.force_regenerate)
            except GenerationError as e:
                raise _Abort("scenario-gen", str(e))
            _track(llm_gen.prompt_tokens, llm_gen.output_tokens, llm_gen.llm_duration_s)
            merged = merge(llm_gen.scenarios, manual_gen.scenarios)
            generation = llm_gen
            generation.scenarios = merged.scenarios
            generation.source = "hybrid"
            detail = f"{len(merged.scenarios)} total after merge"
            if merged.dropped_duplicates:
                detail += f" ({len(merged.dropped_duplicates)} generated duplicate(s) shadowed by manual)"
            events.stage_ok("scenario-gen", detail)
    elif params.scenarios_file:
        try:
            generation = load_scenarios(params.scenarios_file)
        except AuthoringError as e:
            raise _Abort("scenario-load", str(e))
        events.stage_ok("scenario-load", f"{len(generation.scenarios)} scenario(s) from {params.scenarios_file.name}")
    else:
        if intent_result.vague:
            raise _Abort(
                "scenario-gen",
                f"intent too vague: {intent_result.vague_reason}. "
                "Pass --intent, or author scenarios with: verdict plan --manual",
                status="skipped",
            )
        try:
            with events.working(f"asking {config.model} for scenarios..."):
                generation = generate(intent_result, config, repo=repo, force=params.force_regenerate)
        except GenerationError as e:
            raise _Abort("scenario-gen", str(e))
        _track(generation.prompt_tokens, generation.output_tokens, generation.llm_duration_s)
        cache_note = "  [dim](cached)[/]" if generation.from_cache else ""
        events.stage_ok("scenario-gen", f"{len(generation.scenarios)} scenario(s) generated{cache_note}")
    for s in generation.scenarios:
        events.scenario_line(s.name)

    # [4/6] validate
    validations = validate(generation.scenarios, intent_result.diff, intent_result.intent)
    kept = [v.scenario for v in validations if v.traceable]
    dropped = [v for v in validations if not v.traceable]
    if not kept:
        events.stage_warn("validate", "0 scenarios traceable to this diff")
        for v in dropped:
            events.dropped_scenario(v.scenario.name, v.reason, cross_glyph=True)
        raise _Unverified("validate", "no generated scenario was traceable to this diff", generation)
    events.stage_ok("validate", f"{len(kept)}/{len(validations)} traceable to the diff")
    for v in dropped:
        events.dropped_scenario(v.scenario.name, v.reason, cross_glyph=False)
    cap_dropped = kept[params.max_scenarios:]
    kept = kept[:params.max_scenarios]
    if cap_dropped:
        # A capped scenario never becomes a SandboxResult at all - invisible
        # to score(), format_json, and the run record alike unless something
        # says so explicitly. A silent drop here is worse than a FAILED: the
        # report would look identical to a run that verified everything.
        events.stage_warn(
            "validate",
            f"running {len(kept)} of {len(kept) + len(cap_dropped)} traceable scenario(s) - "
            f"--max-scenarios={params.max_scenarios} cap reached, NOT run: {', '.join(s.name for s in cap_dropped)}",
        )

    # [5/6] sandbox (testgen + execution)
    tests, ungeneratable = [], []
    for s in kept:
        try:
            with events.working(f"writing check for {s.name}..."):
                t = generate_test_code(s, intent_result, config)
            _track(t.prompt_tokens, t.output_tokens, t.llm_duration_s)
            tests.append(t)
            events.stage_note("testgen", f"{s.name} [dim](attempt {t.attempts})[/]")
        except GenerationError:
            ungeneratable.append(s)
            _track(0, 0, 0)
            events.stage_warn("testgen", f"{s.name}: could not produce a sound check - skipped")

    if not tests:
        raise _Unverified("testgen", "no scenario produced runnable test code", generation)

    try:
        with events.working("running scenarios in sandbox containers..."):
            results = run_all(
                tests, repo, timeout=params.timeout,
                on_result=lambda r: events.result_line(r.scenario_name, r.status, r.duration_s),
                max_workers=params.sandbox_concurrency,
            )
    except SandboxError as e:
        raise _Abort("sandbox", str(e))

    # [5.5/6] confirm FAILED results independently. A FAILED scenario is the
    # most consequential outcome - it raises risk and can block a push - so a
    # bug in the generated TEST (not the code under test) must never
    # masquerade as a real failure. Only failing scenarios pay this extra
    # generation+sandbox cost; passed/uncertain/error results are untouched.
    downgraded: list[str] = []
    for result in results:
        if result.status != "failed":
            continue
        matching_test = next((t for t in tests if t.scenario.name == result.scenario_name), None)
        if matching_test is None:
            continue
        try:
            with events.working(f"confirming failure for {result.scenario_name}..."):
                confirm_test = generate_test_code(matching_test.scenario, intent_result, config)
            _track(confirm_test.prompt_tokens, confirm_test.output_tokens, confirm_test.llm_duration_s)
            confirm_result = run_test(confirm_test, repo, timeout=params.timeout)
        except GenerationError:
            continue  # could not regenerate - keep the original result as-is
        if confirm_result.status == "failed":
            events.stage_note("confirm", f"{result.scenario_name}: failure reproduced independently")
        else:
            result.status = "uncertain"
            downgraded.append(result.scenario_name)
            events.stage_warn(
                "confirm",
                f"{result.scenario_name}: independent regeneration did not reproduce the failure "
                "- downgraded to uncertain (likely a bug in the generated test, not the code)",
            )

    # [6/6] score + record
    risk = score(results)
    events.stage_ok("score", risk.level)

    record = build_record(run_id, intent_result, generation, results, risk, llm.model_id(config), tokens)
    record["scope"] = params.scope
    if ungeneratable:
        record["ungeneratable"] = [s.name for s in ungeneratable]
    if downgraded:
        record["failure_not_reproduced"] = downgraded
    if cap_dropped:
        record["scenario_cap_dropped"] = [s.name for s in cap_dropped]
    save_run(record, repo)
    audit.append(
        "run_completed",
        {
            "risk": risk.level,
            "passed": risk.passed,
            "failed": risk.failed,
            "inconclusive": risk.inconclusive,
            "tokens": tokens,
        },
        run_id=run_id,
        root=repo,
    )
    return PipelineOutcome(status="completed", record=record, run_id=run_id)
