"""Module 8 - CLI. The full pipeline as `verdict <command>` with staged visibility."""
import hashlib
import shlex
import subprocess
import sys
import time
from pathlib import Path

import click
import typer

# Output must never kill a verdict: LLM-generated tests can print any unicode,
# and Windows pipes default to cp1252. Replace unencodable characters instead
# of crashing after the run already succeeded.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, OSError):
        pass

from dataclasses import asdict

from verdict import audit, hooks, llm, ui
from verdict.authoring import AuthoringError, load_scenarios, write_template
from verdict.config import Config, is_initialized, load_config, save_config
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


def _display_intent_line(intent: str) -> str:
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
from verdict.reporter import (
    build_incomplete_record,
    build_record,
    format_json,
    latest_run_id,
    list_runs,
    load_run,
    new_run_id,
    save_html,
    save_run,
)
from verdict.sandbox import SandboxError, check_docker, run_all, run_test
from verdict.scorer import score
from verdict.testgen import generate_test_code
from verdict.validator import validate

app = typer.Typer(add_completion=False, help="Verdict - proves code does what it claims, before a human reviews it.")


@app.callback(invoke_without_command=True)
def _main(ctx: typer.Context):
    """With no subcommand, drop into the interactive verdict shell."""
    if ctx.invoked_subcommand is None:
        _shell()


def _shell() -> None:
    config = load_config()
    with ui.working("starting verdict..."):
        llm_ok = llm.check(config).reachable
        docker_ok = check_docker()
    ui.shell_banner(config.model, config.provider, llm_ok, docker_ok)

    group = typer.main.get_command(app)
    while True:
        try:
            line = ui.console.input("[bold cyan]verdict[/] [dim]>[/] ").strip()
        except (KeyboardInterrupt, EOFError):
            ui.console.print("\n  [dim]bye - every verdict stays in .verdict/runs/[/]")
            return
        if not line:
            continue
        if line in ("exit", "quit", "q"):
            ui.console.print("  [dim]bye - every verdict stays in .verdict/runs/[/]")
            return
        if line == "help":
            ui.shell_help()
            continue
        if line == "clear":
            ui.console.clear()
            continue
        try:
            args = shlex.split(line)
        except ValueError as e:
            ui.stage_fail("parse", str(e))
            continue
        try:
            group.main(args=args, prog_name="verdict", standalone_mode=False)
        except (typer.Exit, click.exceptions.Exit):
            pass  # commands signal exit codes; the shell lives on
        except click.ClickException as e:
            ui.stage_fail("usage", e.format_message())
        except KeyboardInterrupt:
            ui.console.print("\n  [yellow]interrupted[/]")
        ui.console.print()


def _fail(stage: str, message: str) -> None:
    ui.stage_fail(stage, message)
    raise typer.Exit(code=1)


def _extract(repo: Path, ref: str | None, base: str | None, intent: str | None, paths: list[str] | None = None) -> IntentResult:
    if base:
        return extract_from_range(repo, base, ref or "HEAD", intent=intent, paths=paths)
    if ref:
        return extract_from_commit(repo, ref, paths=paths)
    if intent is not None:
        return extract_from_working_tree(repo, intent, paths=paths)
    return extract_from_commit(repo, "HEAD", paths=paths)


def _masked_config(data: dict) -> dict:
    """API keys never land in the audit log or on screen - only enough to identify them."""
    masked = dict(data)
    key = masked.get("api_key") or ""
    if key:
        masked["api_key"] = f"****{key[-4:]}" if len(key) > 4 else "****"
    return masked


MODEL_HINTS = {
    "openrouter": "e.g. --model qwen/qwen-2.5-coder-32b-instruct",
    "groq": "e.g. --model llama-3.3-70b-versatile",
    "gemini": "e.g. --model gemini-2.0-flash",
    "openai": "e.g. --model gpt-4o-mini",
    "custom": "whatever your endpoint serves",
}


@app.command()
def init(
    model: str = typer.Option(None, help="Model to use (defaults to qwen2.5-coder:7b for ollama)"),
    ollama_url: str = typer.Option(None, help="Ollama server URL (defaults to http://localhost:11434)"),
    provider: str = typer.Option(None, help="LLM provider: ollama (local, default) | openrouter | groq | gemini | openai | custom"),
    api_key: str = typer.Option(None, "--api-key", help="API key for a cloud provider (or set the VERDICT_API_KEY env var instead)"),
    base_url: str = typer.Option(None, "--base-url", help="Custom OpenAI-compatible endpoint (required for provider=custom)"),
):
    """Module 1: one-time setup - writes .verdict/config.json and checks the LLM provider is ready.

    Pick a provider right here at setup - local Ollama (default, diffs never
    leave the machine) or a cloud API key in one shot, e.g.:
      verdict init --provider groq --model llama-3.3-70b-versatile --api-key <key>
    """
    existing = load_config()
    if provider and provider not in llm.PROVIDERS:
        _fail("config", f"unknown provider '{provider}' (valid: {', '.join(llm.PROVIDERS)})")
    resolved_provider = provider or existing.provider
    resolved_model = model or existing.model

    switching_to_cloud = provider and provider != "ollama" and provider != existing.provider
    if switching_to_cloud and model is None:
        hint = MODEL_HINTS.get(resolved_provider, "")
        _fail("config", f"provider '{resolved_provider}' needs --model ({hint})")

    config = Config(
        model=resolved_model,
        ollama_url=ollama_url or existing.ollama_url,
        provider=resolved_provider,
        api_key=api_key or existing.api_key,
        base_url=base_url or existing.base_url,
    )
    path = save_config(config)
    audit.append(
        "config_change",
        {"before": _masked_config(asdict(existing)), "after": _masked_config(asdict(config))},
    )
    ui.stage_ok("config", f"{path}  [dim]model:[/] {config.model}  [dim]provider:[/] {config.provider}")
    if config.provider != "ollama":
        ui.stage_warn("privacy", "cloud provider selected: diffs and intents will leave this machine")

    with ui.working(f"checking {config.provider}..."):
        status = llm.check(config)
    if not status.reachable:
        hint = " - is 'ollama serve' running?" if llm.is_local(config) else f" ({status.error})"
        _fail(config.provider, f"not reachable{hint}")
    if llm.is_local(config) and status.model_known is False:
        ui.stage_warn(config.provider, f"reachable, but model '{config.model}' not pulled yet")
        ui.console.print(f"      [dim]run:[/] [cyan]ollama pull {config.model}[/]")
        raise typer.Exit(code=1)
    ui.stage_ok(config.provider, f"reachable, model '{config.model}' ready")


@app.command()
def health():
    """Liveness check across dependencies - honest, never faked."""
    config = load_config()
    exit_code = 0

    if is_initialized():
        ui.stage_ok("config", f"model: {config.model}  provider: {config.provider}")
    else:
        ui.stage_warn("config", "no .verdict/config.json - run 'verdict init' first")

    with ui.working(f"checking {config.provider}..."):
        status = llm.check(config)
    if not status.reachable:
        where = f"at {config.ollama_url}" if llm.is_local(config) else f"({status.error})"
        ui.stage_fail(config.provider, f"not reachable {where}")
        exit_code = 1
    elif llm.is_local(config) and status.model_known is False:
        ui.stage_warn(config.provider, f"reachable, but model '{config.model}' not pulled")
        exit_code = 1
    else:
        detail = f"{config.model} ready"
        if not llm.is_local(config):
            detail += "  [yellow](cloud - diffs leave this machine)[/]"
        elif status.models:
            detail += f", {len(status.models)} model(s) available"
        ui.stage_ok(config.provider, detail)

    with ui.working("checking Docker..."):
        docker_ok = check_docker()
    if docker_ok:
        ui.stage_ok("docker", "daemon reachable")
    else:
        ui.stage_fail("docker", "daemon not reachable - is Docker Desktop running?")
        exit_code = 1

    raise typer.Exit(code=exit_code)


@app.command()
def plan(
    ref: str = typer.Option(None, help="Commit to verify (default: HEAD)"),
    base: str = typer.Option(None, help="Verify the range base..HEAD instead of one commit"),
    intent: str = typer.Option(None, help="Explicit intent (required for uncommitted changes)"),
    path: list[str] = typer.Option(None, "--path", help="Only verify these files/folders (repeatable)"),
    manual: bool = typer.Option(False, "--manual", help="Write an editable scenario template instead of generating"),
):
    """Dry-run: show scenarios without executing them. --manual writes a template file."""
    repo = Path.cwd()
    config = load_config()

    try:
        intent_result = _extract(repo, ref, base, intent, paths=path)
    except GitError as e:
        _fail("intent", str(e))

    if manual:
        target = repo / ".verdict" / "scenarios" / "scenarios.yaml"
        try:
            path = write_template(target, intent=intent_result.intent)
        except AuthoringError as e:
            _fail("plan", str(e))
        ui.stage_ok("plan", f"created {path}")
        ui.console.print(f"      [dim]edit it, then:[/] [cyan]verdict run --scenarios {path}[/]")
        return

    ui.stage_ok("intent", f'"{_display_intent_line(intent_result.intent)}"')
    if intent_result.vague:
        _fail("scenario-gen", f"intent too vague: {intent_result.vague_reason}")

    try:
        with ui.working(f"asking {config.model} for scenarios..."):
            generation = generate(intent_result, config)
    except GenerationError as e:
        _fail("scenario-gen", str(e))
    ui.stage_ok("scenario-gen", f"{len(generation.scenarios)} scenario(s)")

    validations = validate(generation.scenarios, intent_result.diff, intent_result.intent)
    kept = [v for v in validations if v.traceable]
    ui.stage_ok("validate", f"{len(kept)}/{len(validations)} traceable to the diff")
    for v in validations:
        if v.traceable:
            ui.scenario_line(v.scenario.name, v.scenario.description)
        else:
            ui.console.print(f"      [red]{ui.CROSS}[/] [dim strike]{v.scenario.name}[/] [red dim]{v.reason[:70]}[/]")


@app.command()
def run(
    ref: str = typer.Option(None, help="Commit to verify (default: HEAD)"),
    base: str = typer.Option(None, help="Verify the range base..HEAD instead of one commit"),
    intent: str = typer.Option(None, help="Explicit intent (required for uncommitted changes)"),
    path: list[str] = typer.Option(None, "--path", help="Only verify these files/folders (repeatable)"),
    scenarios_file: Path = typer.Option(None, "--scenarios", help="Run developer-authored scenarios (Module 3b)"),
    hybrid: bool = typer.Option(False, "--hybrid", help="Combine generated + manual scenarios, deduped (needs --scenarios)"),
    max_scenarios: int = typer.Option(4, help="Cap on scenarios executed per run"),
    timeout: int = typer.Option(300, help="Per-scenario sandbox timeout (seconds)"),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
):
    """The full pipeline: intent -> scenarios -> validate -> sandbox -> score -> report."""
    repo = Path.cwd()
    config = load_config()
    if hybrid and scenarios_file is None:
        ui.stage_fail("config", "--hybrid needs --scenarios <file> to know which manual scenarios to merge")
        raise typer.Exit(code=1)
    mode = "hybrid" if hybrid else ("manual" if scenarios_file else "autonomous")
    run_id = new_run_id()
    intent_result: IntentResult | None = None
    tokens = {"llm_calls": 0, "prompt_tokens": 0, "output_tokens": 0, "llm_seconds": 0.0}

    def _track(prompt_tokens: int, output_tokens: int, seconds: float) -> None:
        tokens["llm_calls"] += 1
        tokens["prompt_tokens"] += prompt_tokens
        tokens["output_tokens"] += output_tokens
        tokens["llm_seconds"] = round(tokens["llm_seconds"] + seconds, 2)

    def _abort(stage: str, message: str, status: str = "errored") -> None:
        """A run that dies still leaves a record and an audit entry."""
        record = build_incomplete_record(
            run_id, status, stage, message, llm.model_id(config), intent_result, tokens
        )
        save_run(record, repo)
        audit.append(f"run_{status}", {"stage": stage, "reason": message}, run_id=run_id, root=repo)
        ui.stage_fail(stage, message)
        ui.console.print(f"  [dim]recorded as {status}: run {run_id}[/]")
        raise typer.Exit(code=1)

    def _finish_unverified(stage: str, note: str, generation) -> None:
        """The pipeline completed but produced zero conclusive evidence.
        That is a verdict (UNVERIFIED), not an infrastructure error."""
        risk = score([])
        risk.reasons.insert(0, f"{stage}: {note}")
        ui.stage_ok("score", risk.level)
        record = build_record(run_id, intent_result, generation, [], risk, llm.model_id(config), tokens)
        record["note"] = note
        save_run(record, repo)
        audit.append(
            "run_completed",
            {"risk": risk.level, "passed": 0, "failed": 0, "inconclusive": 0, "note": note, "tokens": tokens},
            run_id=run_id,
            root=repo,
        )
        if as_json:
            typer.echo(format_json(record))
        else:
            ui.verdict_panel(record)
        raise typer.Exit(code=1)

    audit.append(
        "run_started",
        {"mode": mode, "ref": ref, "base": base, "explicit_intent": bool(intent), "paths": list(path or [])},
        run_id=run_id,
        root=repo,
    )

    if not as_json:
        ui.banner(mode, config.model, config.provider)

    # [1/6] config
    with ui.working("checking dependencies..."):
        status = llm.check(config)
        docker_ok = check_docker()
    needs_llm = scenarios_file is None
    if needs_llm and not status.reachable:
        _abort("config", f"{config.provider} not reachable: {status.error or 'no response'}")
    if needs_llm and llm.is_local(config) and status.model_known is False:
        _abort("config", f"model '{config.model}' not pulled")
    if not docker_ok:
        _abort("config", "Docker daemon not reachable")
    ui.stage_ok("config", f"{config.provider} and Docker ready")

    # [2/6] intent
    try:
        intent_result = _extract(repo, ref, base, intent, paths=path)
    except GitError as e:
        _abort("intent", str(e))
    if not intent_result.diff.strip():
        scope = f" under {', '.join(path)}" if path else ""
        _abort("intent", f"diff is empty{scope} - nothing to verify", status="skipped")
    ui.stage_ok("intent", f'"{_display_intent_line(intent_result.intent)}"')
    if path:
        ui.stage_note("scope", f"only verifying: {', '.join(path)}")

    # [3/6] scenarios (generate, load, or hybrid-merge)
    if scenarios_file and hybrid:
        try:
            manual_gen = load_scenarios(scenarios_file)
        except AuthoringError as e:
            _abort("scenario-load", str(e))
        ui.stage_ok("scenario-load", f"{len(manual_gen.scenarios)} manual scenario(s) from {scenarios_file.name}")
        if intent_result.vague:
            ui.stage_warn("scenario-gen", f"intent too vague to generate ({intent_result.vague_reason}) - manual only")
            generation = manual_gen
        else:
            try:
                with ui.working(f"asking {config.model} for scenarios..."):
                    llm_gen = generate(intent_result, config)
            except GenerationError as e:
                _abort("scenario-gen", str(e))
            _track(llm_gen.prompt_tokens, llm_gen.output_tokens, llm_gen.llm_duration_s)
            merged = merge(llm_gen.scenarios, manual_gen.scenarios)
            generation = llm_gen
            generation.scenarios = merged.scenarios
            generation.source = "hybrid"
            detail = f"{len(merged.scenarios)} total after merge"
            if merged.dropped_duplicates:
                detail += f" ({len(merged.dropped_duplicates)} generated duplicate(s) shadowed by manual)"
            ui.stage_ok("scenario-gen", detail)
    elif scenarios_file:
        try:
            generation = load_scenarios(scenarios_file)
        except AuthoringError as e:
            _abort("scenario-load", str(e))
        ui.stage_ok("scenario-load", f"{len(generation.scenarios)} scenario(s) from {scenarios_file.name}")
    else:
        if intent_result.vague:
            _abort(
                "scenario-gen",
                f"intent too vague: {intent_result.vague_reason}. "
                "Pass --intent, or author scenarios with: verdict plan --manual",
                status="skipped",
            )
        try:
            with ui.working(f"asking {config.model} for scenarios..."):
                generation = generate(intent_result, config)
        except GenerationError as e:
            _abort("scenario-gen", str(e))
        _track(generation.prompt_tokens, generation.output_tokens, generation.llm_duration_s)
        ui.stage_ok("scenario-gen", f"{len(generation.scenarios)} scenario(s) generated")
    for s in generation.scenarios:
        ui.scenario_line(s.name)

    # [4/6] validate
    validations = validate(generation.scenarios, intent_result.diff, intent_result.intent)
    kept = [v.scenario for v in validations if v.traceable]
    dropped = [v for v in validations if not v.traceable]
    if not kept:
        ui.stage_warn("validate", "0 scenarios traceable to this diff")
        for v in dropped:
            ui.console.print(f"      [red]{ui.CROSS}[/] [dim strike]{v.scenario.name}[/] [red dim]{v.reason[:70]}[/]")
        _finish_unverified("validate", "no generated scenario was traceable to this diff", generation)
    ui.stage_ok("validate", f"{len(kept)}/{len(validations)} traceable to the diff")
    for v in dropped:
        ui.console.print(f"      [red]x[/] [dim strike]{v.scenario.name}[/] [red dim]{v.reason[:70]}[/]")
    kept = kept[:max_scenarios]

    # [5/6] sandbox (testgen + execution)
    tests, ungeneratable = [], []
    for s in kept:
        try:
            with ui.working(f"writing check for {s.name}..."):
                t = generate_test_code(s, intent_result, config)
            _track(t.prompt_tokens, t.output_tokens, t.llm_duration_s)
            tests.append(t)
            ui.stage_note("testgen", f"{s.name} [dim](attempt {t.attempts})[/]")
        except GenerationError:
            ungeneratable.append(s)
            _track(0, 0, 0)
            ui.stage_warn("testgen", f"{s.name}: could not produce a sound check - skipped")

    if not tests:
        _finish_unverified("testgen", "no scenario produced runnable test code", generation)

    try:
        with ui.working("running scenarios in sandbox containers..."):
            results = run_all(
                tests, repo, timeout=timeout,
                on_result=lambda r: ui.result_line(r.scenario_name, r.status, r.duration_s),
            )
    except SandboxError as e:
        _abort("sandbox", str(e))

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
            with ui.working(f"confirming failure for {result.scenario_name}..."):
                confirm_test = generate_test_code(matching_test.scenario, intent_result, config)
            _track(confirm_test.prompt_tokens, confirm_test.output_tokens, confirm_test.llm_duration_s)
            confirm_result = run_test(confirm_test, repo, timeout=timeout)
        except GenerationError:
            continue  # could not regenerate - keep the original result as-is
        if confirm_result.status == "failed":
            ui.stage_note("confirm", f"{result.scenario_name}: failure reproduced independently")
        else:
            result.status = "uncertain"
            downgraded.append(result.scenario_name)
            ui.stage_warn(
                "confirm",
                f"{result.scenario_name}: independent regeneration did not reproduce the failure "
                "- downgraded to uncertain (likely a bug in the generated test, not the code)",
            )

    # [6/6] score + report
    risk = score(results)
    ui.stage_ok("score", risk.level)

    record = build_record(run_id, intent_result, generation, results, risk, llm.model_id(config), tokens)
    if ungeneratable:
        record["ungeneratable"] = [s.name for s in ungeneratable]
    if downgraded:
        record["failure_not_reproduced"] = downgraded
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

    if as_json:
        typer.echo(format_json(record))
    else:
        ui.verdict_panel(record)
    raise typer.Exit(code=0 if risk.level == "LOW" else 1)


INTENT_TEMPLATE = """\
# What are you building right now? Verdict reads this file live while watching.
# Lines starting with # are ignored. Update it as your goal changes.
# Example:  limit login attempts to 5 per minute per account, reject with 429
"""


def _working_tree_fingerprint(repo: Path, paths: list[str] | None = None) -> tuple[str, bool]:
    """Cheap, deterministic snapshot of 'what the working tree looks like right now'.
    .verdict/ is excluded so verdict's own run records never re-trigger a watch."""
    kwargs = dict(cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace")
    scope = paths or ["."]
    status = subprocess.run(["git", "status", "--porcelain", "--", *scope], **kwargs).stdout
    diff = subprocess.run(["git", "diff", "HEAD", "--", *scope, ":(exclude).verdict"], **kwargs).stdout

    h = hashlib.sha1(diff.encode("utf-8"))
    has_changes = bool(diff.strip())
    for line in status.splitlines():
        path_part = line[3:].strip().strip('"')
        if path_part.replace("\\", "/").startswith(".verdict"):
            continue
        has_changes = True
        h.update(line.encode("utf-8"))
        if line.startswith("??"):
            # untracked content never shows in a diff - fold in size + mtime
            try:
                st = (repo / path_part).stat()
                h.update(f"{st.st_size}:{st.st_mtime_ns}".encode())
            except OSError:
                pass
    return h.hexdigest(), has_changes


@app.command()
def watch(
    intent: str = typer.Option(None, help="What the current work is trying to do (else .verdict/INTENT.md is read, live)"),
    path: list[str] = typer.Option(None, "--path", help="Only watch and verify these files/folders (repeatable)"),
    settle: float = typer.Option(8.0, help="Seconds of working-tree silence before a verification fires"),
    interval: float = typer.Option(2.0, help="Poll interval (seconds)"),
    max_scenarios: int = typer.Option(3, help="Cap on scenarios per triggered run"),
    timeout: int = typer.Option(240, help="Per-scenario sandbox timeout (seconds)"),
):
    """Live mode: watch the working tree while you (or your agent) build.

    Stays silent while files are actively changing - no wasted checks on
    half-written code. Once things settle, the full pipeline runs against
    the uncommitted diff. Ctrl+C to stop.
    """
    repo = Path.cwd()
    intent_file = repo / ".verdict" / "INTENT.md"
    if intent is None and not intent_file.exists():
        intent_file.parent.mkdir(parents=True, exist_ok=True)
        intent_file.write_text(INTENT_TEMPLATE, encoding="utf-8")
        ui.stage_note("watch", f"created {intent_file.relative_to(repo)} - keep it updated with what you're building")

    def resolve_intent() -> str:
        if intent is not None:
            return intent.strip()
        if intent_file.exists():
            lines = intent_file.read_text(encoding="utf-8").splitlines()
            return "\n".join(l for l in lines if not l.lstrip().startswith("#")).strip()
        return ""

    where = ", ".join(path) if path else repo.name
    ui.stage_ok("watch", f"watching [bold]{where}[/] - verifies after {settle:g}s of silence  [dim](ctrl+c to stop)[/]")

    last_fp: str | None = None
    stable_since = time.monotonic()
    last_attempt: tuple[str, str] | None = None
    churning = False
    verifications = 0
    try:
        while True:
            fp, has_changes = _working_tree_fingerprint(repo, path)
            now = time.monotonic()
            if fp != last_fp:
                if last_fp is not None and not churning:
                    ui.stage_note("watch", "activity detected - waiting for it to settle")
                churning = last_fp is not None
                last_fp = fp
                stable_since = now
            elif has_changes and (now - stable_since) >= settle:
                current_intent = resolve_intent()
                key = (fp, current_intent)
                if key != last_attempt:
                    last_attempt = key
                    churning = False
                    if not current_intent:
                        ui.stage_warn("watch", f"changes settled, but no intent - write it in {intent_file.relative_to(repo)} or pass --intent")
                    elif (reason := check_vagueness(current_intent)) is not None:
                        ui.stage_warn("watch", f"changes settled, but intent too vague to verify: {reason}")
                    else:
                        ui.console.print()
                        ui.stage_ok("watch", f"settled for {settle:g}s - verifying the working tree")
                        try:
                            run(
                                ref=None, base=None, intent=current_intent, path=path,
                                scenarios_file=None, hybrid=False,
                                max_scenarios=max_scenarios, timeout=timeout, as_json=False,
                            )
                        except typer.Exit:
                            pass  # every outcome is already recorded; the watch lives on
                        verifications += 1
                        ui.stage_note("watch", "back to watching - next change starts a new cycle")
            time.sleep(interval)
    except KeyboardInterrupt:
        ui.console.print()
        ui.stage_ok("watch", f"stopped - {verifications} verification(s) this session, all in .verdict/runs/")


config_app = typer.Typer(add_completion=False, help="Read or change verdict settings for this repo.")
app.add_typer(config_app, name="config")

_CONFIG_KEYS = ("model", "ollama_url", "provider", "api_key", "base_url")


@config_app.command("get")
def config_get(key: str = typer.Argument(None, help="Setting to read (omit for all)")):
    """Show current settings (API keys are always masked)."""
    config = load_config()
    shown = _masked_config(asdict(config))
    if key is None:
        for k in _CONFIG_KEYS:
            ui.console.print(f"  [cyan]{k}[/] = {shown[k]}")
        return
    if key not in _CONFIG_KEYS:
        _fail("config", f"unknown key '{key}' (valid: {', '.join(_CONFIG_KEYS)})")
    ui.console.print(f"  [cyan]{key}[/] = {shown[key]}")


@config_app.command("set")
def config_set(key: str, value: str):
    """Change a setting - the change is audit-logged (keys are masked in the log)."""
    if key not in _CONFIG_KEYS:
        _fail("config", f"unknown key '{key}' (valid: {', '.join(_CONFIG_KEYS)})")
    if key == "provider" and value not in llm.PROVIDERS:
        _fail("config", f"unknown provider '{value}' (valid: {', '.join(llm.PROVIDERS)})")
    config = load_config()
    before = asdict(config)
    setattr(config, key, value)
    save_config(config)
    audit.append("config_change", {"before": _masked_config(before), "after": _masked_config(asdict(config))})
    shown = _masked_config({key: value})[key] if key == "api_key" else value
    ui.stage_ok("config", f"{key} = {shown}")
    if key == "provider" and value != "ollama":
        ui.stage_warn("privacy", "cloud provider: diffs and intents WILL leave this machine")
        ui.console.print(f"      [dim]set your key:[/] [cyan]verdict config set api_key <key>[/] [dim](or {llm.API_KEY_ENV} env var)[/]")


@app.command("install-hook")
def install_hook():
    """Install the git pre-push hook: every push is verified before it leaves this machine."""
    try:
        path = hooks.install(Path.cwd())
    except hooks.HookError as e:
        _fail("hook", str(e))
    ui.stage_ok("hook", f"pre-push hook installed at {path}")
    ui.console.print("      [dim]every push now runs verdict on exactly the commits being pushed;[/]")
    ui.console.print("      [dim]non-LOW verdicts block the push (bypass: git push --no-verify)[/]")


@app.command("uninstall-hook")
def uninstall_hook():
    """Remove the verdict pre-push hook (refuses to touch hooks it didn't install)."""
    try:
        hooks.uninstall(Path.cwd())
    except hooks.HookError as e:
        _fail("hook", str(e))
    ui.stage_ok("hook", "pre-push hook removed")


def _resolve_run_id(run_id: str) -> str:
    """'last' always means the newest run - nobody should have to copy ids around."""
    if run_id != "last":
        return run_id
    latest = latest_run_id()
    if latest is None:
        _fail("runs", "no runs recorded yet under .verdict/runs/")
    return latest


@app.command()
def runs(limit: int = typer.Option(15, help="How many recent runs to show")):
    """Browse past verdicts as a table - the history without touching JSON."""
    records = list_runs(limit=limit)
    if not records:
        ui.stage_warn("runs", "no runs recorded yet - run 'verdict run' first")
        return
    ui.runs_table(records)
    ui.console.print("  [dim]details:[/] [cyan]verdict logs <run-id>[/]   [dim]shareable page:[/] [cyan]verdict report <run-id>[/]   [dim]('last' works as an id)[/]")


@app.command()
def report(
    run_id: str = typer.Argument("last", help="Run to export ('last' = newest)"),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open the report in your browser"),
):
    """Export a run as a self-contained HTML page - readable, shareable, no JSON."""
    record = load_run(_resolve_run_id(run_id))
    if record is None:
        _fail("report", f"no run named {run_id} under .verdict/runs/")
    path = save_html(record)
    ui.stage_ok("report", f"{path}")
    if open_browser:
        import webbrowser

        webbrowser.open(path.as_uri())


@app.command()
def logs(run_id: str = typer.Argument("last", help="Run to inspect ('last' = newest)")):
    """Full evidence for a run: prompt, test code, sandbox output - the audit trail."""
    record = load_run(_resolve_run_id(run_id))
    if record is None:
        _fail("logs", f"no run named {run_id} under .verdict/runs/")
    ui.console.print(f"[bold cyan]run[/]     {record['run_id']}  [dim]({record['created_at']})[/]")
    ui.console.print(f"[bold cyan]model[/]   {record['model']}")
    intent_txt = record.get("intent") or "(never extracted)"
    ui.console.print(f"[bold cyan]intent[/]  {intent_txt.splitlines()[0]}")
    if record.get("status", "completed") != "completed":
        ui.console.print(f"[bold cyan]status[/]  [yellow]{record['status']}[/] at stage '{record.get('failed_stage', '?')}': {record.get('reason', '')}")
        return
    ui.console.print(f"[bold cyan]risk[/]    {record['risk']['level']}")
    for r in record["results"]:
        tag, style = ui.STATUS_STYLES.get(r["status"], (r["status"].upper(), "white"))
        ui.console.print(f"\n[bold]{ui.RULE} {r['scenario_name']}[/] -> [{style}]{tag}[/] [dim](exit {r['exit_code']}, {r['duration_s']}s)[/]")
        ui.console.print("[dim]-- test code --[/]")
        ui.show_test_code(r["test_code"])
        if r["stdout"].strip():
            ui.console.print("[dim]-- stdout --[/]")
            ui.console.print(r["stdout"].strip()[:2000])
        if r["stderr"].strip():
            ui.console.print("[dim]-- stderr --[/]")
            ui.console.print(f"[red]{r['stderr'].strip()[:2000]}[/]")


if __name__ == "__main__":
    app()
