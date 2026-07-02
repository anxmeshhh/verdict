"""Module 8 - CLI. The full pipeline as `verdict <command>` with staged visibility."""
import shlex
from pathlib import Path

import click
import typer

from verdict import audit, ui
from verdict.authoring import AuthoringError, load_scenarios, write_template
from verdict.config import Config, check_ollama, is_initialized, load_config, save_config
from verdict.generator import GenerationError, generate
from verdict.intent import GitError, IntentResult, extract_from_commit, extract_from_range, extract_from_working_tree
from verdict.reporter import build_incomplete_record, build_record, format_json, load_run, new_run_id, save_run
from verdict.sandbox import SandboxError, check_docker, run_all
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
        ollama_ok = check_ollama(config.ollama_url).reachable
        docker_ok = check_docker()
    ui.shell_banner(config.model, ollama_ok, docker_ok)

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


def _extract(repo: Path, ref: str | None, base: str | None, intent: str | None) -> IntentResult:
    if base:
        return extract_from_range(repo, base, ref or "HEAD", intent=intent)
    if ref:
        return extract_from_commit(repo, ref)
    if intent is not None:
        return extract_from_working_tree(repo, intent)
    return extract_from_commit(repo, "HEAD")


@app.command()
def init(
    model: str = typer.Option(None, help="Ollama model to use (defaults to qwen2.5-coder:7b)"),
    ollama_url: str = typer.Option(None, help="Ollama server URL (defaults to http://localhost:11434)"),
):
    """Module 1: one-time setup - writes .verdict/config.json and checks Ollama is ready."""
    existing = load_config()
    config = Config(model=model or existing.model, ollama_url=ollama_url or existing.ollama_url)
    path = save_config(config)
    audit.append(
        "config_change",
        {"before": {"model": existing.model, "ollama_url": existing.ollama_url},
         "after": {"model": config.model, "ollama_url": config.ollama_url}},
    )
    ui.stage_ok("config", f"{path}  [dim]model:[/] {config.model}")

    with ui.working("checking Ollama..."):
        status = check_ollama(config.ollama_url)
    if not status.reachable:
        _fail("ollama", f"not reachable at {config.ollama_url} - is 'ollama serve' running?")
    if config.model not in status.models:
        ui.stage_warn("ollama", f"reachable, but model '{config.model}' not pulled yet")
        ui.console.print(f"      [dim]run:[/] [cyan]ollama pull {config.model}[/]")
        raise typer.Exit(code=1)
    ui.stage_ok("ollama", f"reachable at {config.ollama_url}, model ready")


@app.command()
def health():
    """Liveness check across dependencies - honest, never faked."""
    config = load_config()
    exit_code = 0

    if is_initialized():
        ui.stage_ok("config", f"model: {config.model}")
    else:
        ui.stage_warn("config", "no .verdict/config.json - run 'verdict init' first")

    with ui.working("checking Ollama..."):
        status = check_ollama(config.ollama_url)
    if not status.reachable:
        ui.stage_fail("ollama", f"not reachable at {config.ollama_url}")
        exit_code = 1
    elif config.model not in status.models:
        ui.stage_warn("ollama", f"reachable, but model '{config.model}' not pulled")
        exit_code = 1
    else:
        ui.stage_ok("ollama", f"{config.model} loaded, {len(status.models)} model(s) available")

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
    manual: bool = typer.Option(False, "--manual", help="Write an editable scenario template instead of generating"),
):
    """Dry-run: show scenarios without executing them. --manual writes a template file."""
    repo = Path.cwd()
    config = load_config()

    try:
        intent_result = _extract(repo, ref, base, intent)
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

    ui.stage_ok("intent", f'"{intent_result.intent.splitlines()[0][:70]}"')
    if intent_result.vague:
        _fail("scenario-gen", f"intent too vague: {intent_result.vague_reason}")

    try:
        with ui.working(f"asking {config.model} for scenarios..."):
            generation = generate(intent_result, config.model, config.ollama_url)
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
            ui.console.print(f"      [red]✗[/] [dim strike]{v.scenario.name}[/] [red dim]{v.reason[:70]}[/]")


@app.command()
def run(
    ref: str = typer.Option(None, help="Commit to verify (default: HEAD)"),
    base: str = typer.Option(None, help="Verify the range base..HEAD instead of one commit"),
    intent: str = typer.Option(None, help="Explicit intent (required for uncommitted changes)"),
    scenarios_file: Path = typer.Option(None, "--scenarios", help="Run developer-authored scenarios (Module 3b)"),
    max_scenarios: int = typer.Option(4, help="Cap on scenarios executed per run"),
    timeout: int = typer.Option(300, help="Per-scenario sandbox timeout (seconds)"),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
):
    """The full pipeline: intent -> scenarios -> validate -> sandbox -> score -> report."""
    repo = Path.cwd()
    config = load_config()
    mode = "manual" if scenarios_file else "autonomous"
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
            run_id, status, stage, message, config.model, intent_result, tokens
        )
        save_run(record, repo)
        audit.append(f"run_{status}", {"stage": stage, "reason": message}, run_id=run_id, root=repo)
        ui.stage_fail(stage, message)
        ui.console.print(f"  [dim]recorded as {status}: run {run_id}[/]")
        raise typer.Exit(code=1)

    audit.append(
        "run_started",
        {"mode": mode, "ref": ref, "base": base, "explicit_intent": bool(intent)},
        run_id=run_id,
        root=repo,
    )

    if not as_json:
        ui.banner(mode, config.model)

    # [1/6] config
    with ui.working("checking dependencies..."):
        status = check_ollama(config.ollama_url)
        docker_ok = check_docker()
    needs_llm = scenarios_file is None
    if needs_llm and not status.reachable:
        _abort("config", f"Ollama not reachable at {config.ollama_url}")
    if needs_llm and config.model not in status.models:
        _abort("config", f"model '{config.model}' not pulled")
    if not docker_ok:
        _abort("config", "Docker daemon not reachable")
    ui.stage_ok("config", "Ollama and Docker ready")

    # [2/6] intent
    try:
        intent_result = _extract(repo, ref, base, intent)
    except GitError as e:
        _abort("intent", str(e))
    if not intent_result.diff.strip():
        _abort("intent", "diff is empty - nothing to verify", status="skipped")
    ui.stage_ok("intent", f'"{intent_result.intent.splitlines()[0][:70]}"')

    # [3/6] scenarios (generate or load)
    if scenarios_file:
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
                generation = generate(intent_result, config.model, config.ollama_url)
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
        _abort("validate", "no scenario is traceable to this diff - nothing trustworthy to run")
    ui.stage_ok("validate", f"{len(kept)}/{len(validations)} traceable to the diff")
    for v in dropped:
        ui.console.print(f"      [red]x[/] [dim strike]{v.scenario.name}[/] [red dim]{v.reason[:70]}[/]")
    kept = kept[:max_scenarios]

    # [5/6] sandbox (testgen + execution)
    tests, ungeneratable = [], []
    for s in kept:
        try:
            with ui.working(f"writing check for {s.name}..."):
                t = generate_test_code(s, intent_result, config.model, config.ollama_url)
            _track(t.prompt_tokens, t.output_tokens, t.llm_duration_s)
            tests.append(t)
            ui.stage_note("testgen", f"{s.name} [dim](attempt {t.attempts})[/]")
        except GenerationError:
            ungeneratable.append(s)
            _track(0, 0, 0)
            ui.stage_warn("testgen", f"{s.name}: could not produce a sound check - skipped")

    if not tests:
        _abort("sandbox", "no scenario produced runnable test code - verdict is UNVERIFIED")

    try:
        with ui.working("running scenarios in sandbox containers..."):
            results = run_all(
                tests, repo, timeout=timeout,
                on_result=lambda r: ui.result_line(r.scenario_name, r.status, r.duration_s),
            )
    except SandboxError as e:
        _abort("sandbox", str(e))

    # [6/6] score + report
    risk = score(results)
    ui.stage_ok("score", risk.level)

    record = build_record(run_id, intent_result, generation, results, risk, config.model, tokens)
    if ungeneratable:
        record["ungeneratable"] = [s.name for s in ungeneratable]
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


@app.command()
def logs(run_id: str):
    """Full evidence for a run: prompt, test code, sandbox output - the audit trail."""
    record = load_run(run_id)
    if record is None:
        _fail("logs", f"no run named {run_id} under .verdict/runs/")
    ui.console.print(f"[bold cyan]run[/]     {record['run_id']}  [dim]({record['created_at']})[/]")
    ui.console.print(f"[bold cyan]model[/]   {record['model']}")
    ui.console.print(f"[bold cyan]intent[/]  {record['intent'].splitlines()[0]}")
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
