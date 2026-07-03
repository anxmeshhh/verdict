"""Module 8 - CLI. The full pipeline as `verdict <command>` with staged visibility."""
import hashlib
import os
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
from verdict.authoring import AuthoringError, write_template
from verdict.config import Config, ensure_gitignore, is_initialized, load_config, save_config
from verdict.generator import GenerationError, generate
from verdict.intent import GitError, check_vagueness
from verdict.pipeline import (
    PipelineParams,
    display_intent_line,
    execute_pipeline,
    extract_intent,
)
from verdict.reporter import (
    format_json,
    latest_run_id,
    list_runs,
    load_run,
    save_html,
)
from verdict.sandbox import check_docker
from verdict.validator import validate


class _CliEvents:
    """Pipeline events rendered exactly as the Phase 1 CLI always has -
    byte-for-byte, verified against a pre-refactor snapshot."""

    def stage_ok(self, name: str, detail: str = "") -> None:
        ui.stage_ok(name, detail)

    def stage_warn(self, name: str, detail: str) -> None:
        ui.stage_warn(name, detail)

    def stage_fail(self, name: str, detail: str) -> None:
        ui.stage_fail(name, detail)

    def stage_note(self, name: str, detail: str) -> None:
        ui.stage_note(name, detail)

    def scenario_line(self, name: str) -> None:
        ui.scenario_line(name)

    def dropped_scenario(self, name: str, reason: str, cross_glyph: bool) -> None:
        mark = ui.CROSS if cross_glyph else "x"
        ui.console.print(f"      [red]{mark}[/] [dim strike]{name}[/] [red dim]{reason[:70]}[/]")

    def result_line(self, scenario_name: str, status: str, duration_s: float) -> None:
        ui.result_line(scenario_name, status, duration_s)

    def recorded_incomplete(self, status: str, run_id: str) -> None:
        ui.console.print(f"  [dim]recorded as {status}: run {run_id}[/]")

    def working(self, message: str):
        return ui.working(message)

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
        if line.startswith("/"):  # "/model" etc. - familiar slash-command feel, same commands underneath
            line = line[1:]
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
        database_url=existing.database_url,
    )
    path = save_config(config)
    audit.append(
        "config_change",
        {"before": _masked_config(asdict(existing)), "after": _masked_config(asdict(config))},
    )
    ui.stage_ok("config", f"{path}  [dim]model:[/] {config.model}  [dim]provider:[/] {config.provider}")
    if config.provider != "ollama":
        ui.stage_warn("privacy", "cloud provider selected: diffs and intents will leave this machine")

    gitignore_path = ensure_gitignore()
    if gitignore_path:
        ui.stage_ok("gitignore", f"{gitignore_path}  [dim]- added .verdict/ (full diffs/prompts live there)[/]")
    else:
        ui.stage_ok("gitignore", ".verdict/ already ignored")

    _verify_llm_ready(config)


def _verify_llm_ready(config: Config) -> None:
    """Live check that the configured provider+model actually works - shared by
    `init` and `model` so both give the same honest, never-faked confirmation."""
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


PROVIDER_ORDER = ["ollama", "openrouter", "groq", "gemini", "openai", "custom"]


def _list_models(config: Config) -> tuple[list[str], str | None]:
    """Live model list for whichever provider is in `config` - never a guessed
    or hardcoded list, always what the server/API itself reports right now."""
    if llm.is_local(config):
        from verdict.config import check_ollama

        status = check_ollama(config.ollama_url)
        if not status.reachable:
            return [], status.error or "ollama not reachable"
        return status.models, None
    status = llm.check(config)
    if not status.reachable:
        return [], status.error
    return status.models, None


def _pick_from_list(models: list[str], current: str) -> str:
    """Numbered picker with type-to-filter, so a 300-model catalog (OpenRouter)
    is still browsable instead of dumping every id on screen at once."""
    filtered = models
    while True:
        shown = filtered[:20]
        for i, m in enumerate(shown, start=1):
            tag = "  [dim](current)[/]" if m == current else ""
            ui.console.print(f"    [cyan]{i:>2}[/]  {m}{tag}")
        if len(filtered) > len(shown):
            ui.console.print(f"    [dim]...and {len(filtered) - len(shown)} more - keep typing to narrow it down[/]")
        raw = ui.console.input(
            "\n  [bold cyan]select model[/] [dim](number, or type to filter)[/] > "
        ).strip()
        if not raw:
            return current
        if raw.isdigit() and 1 <= int(raw) <= len(shown):
            return shown[int(raw) - 1]
        matches = [m for m in filtered if raw.lower() in m.lower()]
        if len(matches) == 1:
            return matches[0]
        if matches:
            filtered = matches
            continue
        ui.stage_warn("model", f"no model matches '{raw}' - try a shorter filter")


@app.command()
def model():
    """Interactive picker: choose a provider, then pick the exact model it
    offers right now - fetched live from the provider, never typed blind.

    The API key prompt is plain text (not masked) on purpose - hidden/getpass
    input silently drops or mangles pasted text on many terminals.
    """
    existing = load_config()
    ui.console.print(f"  [dim]current[/] [cyan]{existing.model}[/] [dim]@[/] [cyan]{existing.provider}[/]\n")

    ui.console.print("  [bold]providers[/]")
    for i, name in enumerate(PROVIDER_ORDER, start=1):
        tag = "  [dim](current)[/]" if name == existing.provider else ""
        ui.console.print(f"    [cyan]{i}[/]  {name}{tag}")
    raw = ui.console.input(
        f"\n  [bold cyan]select provider[/] [dim](1-{len(PROVIDER_ORDER)}, enter to keep '{existing.provider}')[/] > "
    ).strip()
    if not raw:
        provider = existing.provider
    else:
        if not raw.isdigit() or not (1 <= int(raw) <= len(PROVIDER_ORDER)):
            _fail("model", f"enter a number 1-{len(PROVIDER_ORDER)}")
        provider = PROVIDER_ORDER[int(raw) - 1]

    api_key = existing.api_key
    base_url = existing.base_url

    if provider == "custom" and not base_url:
        base_url = ui.console.input("  [bold cyan]base url[/] [dim](OpenAI-compatible endpoint)[/] > ").strip()
        if not base_url:
            _fail("model", "provider 'custom' needs a base url")

    if provider != "ollama":
        import os as _os

        has_key = bool(_os.environ.get(llm.API_KEY_ENV, "").strip() or api_key)
        # Plain (unmasked) input on purpose: getpass-style hidden prompts read
        # the console in raw/char-by-char mode, which on Windows terminals
        # commonly drops or mangles clipboard-pasted text - the key silently
        # comes back empty and the old/invalid one gets kept instead. A
        # visible prompt uses normal line-buffered input, so paste works.
        label = "API key (visible while typing/pasting)" if not has_key else \
            "API key (visible - enter to keep the current one)"
        entered = ui.console.input(f"  [bold cyan]{label}[/] > ").strip().strip("'\"")
        if entered:
            api_key = entered
        elif not has_key:
            _fail("model", f"provider '{provider}' needs an API key - set {llm.API_KEY_ENV} or paste one here")

    candidate = Config(
        model=existing.model, ollama_url=existing.ollama_url,
        provider=provider, api_key=api_key, base_url=base_url,
    )
    with ui.working(f"fetching models from {provider}..."):
        models, error = _list_models(candidate)

    if not models:
        ui.stage_warn("model", f"couldn't list models from {provider}{f' ({error})' if error else ''}")
        model_id = ui.console.input("  [bold cyan]model id[/] [dim](type it exactly)[/] > ").strip()
        if not model_id:
            _fail("model", "no model id given")
    else:
        ui.stage_ok("model", f"{len(models)} model(s) available from {provider}")
        model_id = _pick_from_list(models, existing.model if provider == existing.provider else "")

    config = Config(
        model=model_id, ollama_url=existing.ollama_url, provider=provider,
        api_key=api_key, base_url=base_url, database_url=existing.database_url,
    )
    path = save_config(config)
    audit.append(
        "config_change",
        {"before": _masked_config(asdict(existing)), "after": _masked_config(asdict(config))},
    )
    ui.stage_ok("config", f"{path}  [dim]model:[/] {config.model}  [dim]provider:[/] {config.provider}")
    if config.provider != "ollama":
        ui.stage_warn("privacy", "cloud provider selected: diffs and intents will leave this machine")

    gitignore_path = ensure_gitignore()
    if gitignore_path:
        ui.stage_ok("gitignore", f"{gitignore_path}  [dim]- added .verdict/ (full diffs/prompts live there)[/]")
    else:
        ui.stage_ok("gitignore", ".verdict/ already ignored")

    _verify_llm_ready(config)


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

    # Module 18 extensions - only what's configured gets checked; a plain
    # CLI setup with no Redis is not "unhealthy", it just has fewer parts.
    from verdict import health as health_mod
    from verdict import store

    db_url = store.resolve_database_url(config)
    if db_url:
        pg = health_mod.check_postgres(db_url)
        (ui.stage_ok if pg.ok else ui.stage_fail)("postgres", pg.detail)
        if pg.ok:
            q = health_mod.check_queue(db_url)
            ui.stage_ok("queue", q.detail)
        else:
            exit_code = 1
    if os.environ.get(health_mod.REDIS_URL_ENV, "").strip():
        r = health_mod.check_redis()
        (ui.stage_ok if r.ok else ui.stage_fail)("redis", r.detail)
        if not r.ok:
            exit_code = 1
    disk = health_mod.check_disk()
    (ui.stage_ok if disk.ok else ui.stage_warn)("disk", disk.detail)

    raise typer.Exit(code=exit_code)


@app.command()
def plan(
    ref: str = typer.Option(None, help="Commit to verify (default: HEAD)"),
    base: str = typer.Option(None, help="Verify the range base..HEAD instead of one commit"),
    intent: str = typer.Option(None, help="Explicit intent (required for uncommitted changes)"),
    path: list[str] = typer.Option(None, "--path", help="Only verify these files/folders (repeatable)"),
    manual: bool = typer.Option(False, "--manual", help="Write an editable scenario template instead of generating"),
    force_regenerate: bool = typer.Option(
        False, "--force-regenerate", help="Bypass the scenario-gen cache and ask the model fresh"
    ),
):
    """Dry-run: show scenarios without executing them. --manual writes a template file."""
    repo = Path.cwd()
    config = load_config()

    try:
        intent_result = extract_intent(repo, ref, base, intent, paths=path)
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

    ui.stage_ok("intent", f'"{display_intent_line(intent_result.intent)}"')
    if intent_result.vague:
        _fail("scenario-gen", f"intent too vague: {intent_result.vague_reason}")

    try:
        with ui.working(f"asking {config.model} for scenarios..."):
            generation = generate(intent_result, config, repo=repo, force=force_regenerate)
    except GenerationError as e:
        _fail("scenario-gen", str(e))
    cache_note = "  [dim](cached - same diff+intent+model as a previous run)[/]" if generation.from_cache else ""
    ui.stage_ok("scenario-gen", f"{len(generation.scenarios)} scenario(s){cache_note}")

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
    max_scenarios: int = typer.Option(
        8, help="Cap on scenarios executed per run (autonomous mode asks for 2-5; "
        "--hybrid adds manual ones on top, so the default has headroom for both combined)"
    ),
    timeout: int = typer.Option(300, help="Per-scenario sandbox timeout (seconds)"),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
    force_regenerate: bool = typer.Option(
        False, "--force-regenerate", help="Bypass the scenario-gen cache and ask the model fresh"
    ),
):
    """The full pipeline: intent -> scenarios -> validate -> sandbox -> score -> report.

    Orchestration lives in verdict/pipeline.py (shared with the Phase 3
    worker); this command is the Rich-rendering frontend for it.

    Exit codes (CI contract): 0 = verified LOW risk. 1 = the CODE looks
    risky (MEDIUM/HIGH/UNVERIFIED - an evidence-based verdict). 2 = verdict
    itself could not verify (bad ref, provider down, Docker down, bad
    invocation) - alert the checker's owner, don't blame the code."""
    if as_json:
        # stdout must contain ONLY the final json blob - every progress/status
        # line from here on goes to stderr instead, before anything can print
        ui.route_to_stderr()
    repo = Path.cwd()
    config = load_config()
    if hybrid and scenarios_file is None:
        ui.stage_fail("config", "--hybrid needs --scenarios <file> to know which manual scenarios to merge")
        raise typer.Exit(code=2)
    params = PipelineParams(
        ref=ref, base=base, intent=intent, paths=path,
        scenarios_file=scenarios_file, hybrid=hybrid,
        max_scenarios=max_scenarios, timeout=timeout,
        force_regenerate=force_regenerate,
    )
    _run_and_finish(params, config, repo, as_json)


def _run_and_finish(params: PipelineParams, config: Config, repo: Path, as_json: bool) -> None:
    """Shared tail for run/check: banner, pipeline, output, 3-way exit code."""
    if not as_json:
        ui.banner(params.mode, config.model, config.provider)

    outcome = execute_pipeline(params, config, repo, events=_CliEvents())

    if outcome.status in ("errored", "skipped"):
        # verdict couldn't produce evidence either way - that is NOT the same
        # signal as "the code is risky", and CI must be able to tell them apart
        raise typer.Exit(code=2)
    if as_json:
        typer.echo(format_json(outcome.record))
    else:
        ui.verdict_panel(outcome.record)
    if outcome.status == "unverified":
        raise typer.Exit(code=1)
    raise typer.Exit(code=0 if outcome.risk_level == "LOW" else 1)


@app.command()
def check(
    path: list[str] = typer.Option(None, "--path", help="Only verify these files/folders (repeatable)"),
    max_scenarios: int = typer.Option(8, help="Cap on scenarios executed per run"),
    timeout: int = typer.Option(300, help="Per-scenario sandbox timeout (seconds)"),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
    force_regenerate: bool = typer.Option(
        False, "--force-regenerate", help="Bypass the scenario-gen cache and ask the model fresh"
    ),
):
    """Verify the obvious thing - no flags to think about.

    Uncommitted changes present -> verifies the working tree (intent read
    from .verdict/INTENT.md). Clean tree -> verifies the last commit (intent
    from its message). The chosen scope is printed up front; use `verdict
    run` with explicit flags whenever the inference isn't what you want.
    Exit codes: same contract as `run` (0 LOW / 1 risky / 2 couldn't verify)."""
    if as_json:
        ui.route_to_stderr()
    repo = Path.cwd()
    config = load_config()

    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--", ".", ":(exclude).verdict"],
        cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace",
    ).stdout.strip()

    if dirty:
        intent_file = repo / ".verdict" / "INTENT.md"
        intent_text = ""
        if intent_file.exists():
            lines = intent_file.read_text(encoding="utf-8").splitlines()
            intent_text = "\n".join(l for l in lines if not l.lstrip().startswith("#")).strip()
        if not intent_text:
            ui.stage_fail(
                "check",
                "uncommitted changes found, but no intent to verify them against - "
                f"write what you're building in {intent_file.relative_to(repo)}, "
                "or pass it explicitly: verdict run --intent \"...\"",
            )
            raise typer.Exit(code=2)
        if (reason := check_vagueness(intent_text)) is not None:
            ui.stage_fail("check", f"intent in .verdict/INTENT.md is too vague to verify against: {reason}")
            raise typer.Exit(code=2)
        ui.stage_note("check", "uncommitted changes found - verifying the working tree (intent from .verdict/INTENT.md)")
        params = PipelineParams(intent=intent_text, paths=path, max_scenarios=max_scenarios,
                                timeout=timeout, force_regenerate=force_regenerate)
    else:
        ui.stage_note("check", "working tree clean - verifying the last commit (intent from its message)")
        params = PipelineParams(paths=path, max_scenarios=max_scenarios,
                                timeout=timeout, force_regenerate=force_regenerate)

    _run_and_finish(params, config, repo, as_json)


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

_CONFIG_KEYS = ("model", "ollama_url", "provider", "api_key", "base_url", "database_url")


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


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Bind address (0.0.0.0 inside docker-compose)"),
    port: int = typer.Option(8400, help="Port for the API"),
):
    """Phase 3 API gateway: POST /runs, GET /runs, /health, /metrics.

    Needs the server extra (pip install 'verdict[server]'), a Postgres
    (VERDICT_DATABASE_URL) and a Redis (VERDICT_REDIS_URL) - docker-compose
    up provides all three."""
    try:
        import uvicorn
    except ImportError:
        _fail("serve", "server deps not installed - run: pip install 'verdict[server]'")
    from verdict import store

    if not store.resolve_database_url(load_config()):
        _fail("serve", "server mode needs the data layer - set VERDICT_DATABASE_URL")
    ui.stage_ok("serve", f"http://{host}:{port}  [dim](docs at /docs, health at /health, metrics at /metrics)[/]")
    uvicorn.run("verdict.server.api:app", host=host, port=port, log_level="info")


@app.command()
def worker(
    concurrency: int = typer.Option(
        None, help="Concurrent jobs = concurrent sandbox containers "
        "(default: VERDICT_WORKER_CONCURRENCY env or 2 - the doc's MAX_CONCURRENT_SANDBOX_RUNS)"
    ),
):
    """Phase 3 worker: pulls queued runs and executes the same pipeline the CLI uses."""
    try:
        from verdict.server.queue import DEFAULT_WORKER_CONCURRENCY, WORKER_CONCURRENCY_ENV, celery_app
    except ImportError:
        _fail("worker", "server deps not installed - run: pip install 'verdict[server]'")
    n = concurrency or int(os.environ.get(WORKER_CONCURRENCY_ENV, DEFAULT_WORKER_CONCURRENCY))
    # Windows can't fork: solo pool for n=1, threads otherwise. Linux (the
    # docker-compose case) uses the default prefork pool.
    argv = ["worker", "--loglevel=info", f"--concurrency={n}"]
    if sys.platform == "win32":
        argv.append("--pool=solo" if n == 1 else "--pool=threads")
    ui.stage_ok("worker", f"starting with concurrency={n} [dim](= max concurrent sandbox containers)[/]")
    celery_app.worker_main(argv=argv)


profile_app = typer.Typer(add_completion=False, help="Named provider profiles: set up once, switch by name forever.")
app.add_typer(profile_app, name="profile")

_PROFILE_FIELDS = ("provider", "model", "api_key", "base_url", "ollama_url")


@profile_app.command("save")
def profile_save(name: str = typer.Argument(..., help="Profile name, e.g. 'groq' or 'local'")):
    """Snapshot the CURRENT provider settings under a name.

    Day-to-day switching then never involves typing a secret again -
    `verdict use <name>` applies the whole set (the exact leak path this
    closes: pasting api keys into terminals repeatedly)."""
    config = load_config()
    config.profiles[name] = {k: getattr(config, k) for k in _PROFILE_FIELDS}
    save_config(config)
    audit.append("config_change", {"profile_saved": name})
    ui.stage_ok("profile", f"saved '{name}' = {config.provider} / {config.model}")
    ui.console.print(f"      [dim]switch anytime:[/] [cyan]verdict use {name}[/]")


@profile_app.command("list")
def profile_list():
    """Show saved profiles (keys always masked)."""
    config = load_config()
    if not config.profiles:
        ui.stage_warn("profile", "no profiles saved yet - configure a provider, then: verdict profile save <name>")
        return
    for name, values in config.profiles.items():
        current = all(getattr(config, k) == v for k, v in values.items())
        tag = "  [green](active)[/]" if current else ""
        key_note = f"  [dim]key {_masked_config(values).get('api_key') or 'none'}[/]"
        ui.console.print(f"  [cyan]{name:12}[/] {values.get('provider')} / {values.get('model')}{key_note}{tag}")


@profile_app.command("delete")
def profile_delete(name: str):
    config = load_config()
    if name not in config.profiles:
        _fail("profile", f"no profile named '{name}' (saved: {', '.join(config.profiles) or 'none'})")
    del config.profiles[name]
    save_config(config)
    audit.append("config_change", {"profile_deleted": name})
    ui.stage_ok("profile", f"deleted '{name}'")


@app.command()
def use(name: str = typer.Argument(..., help="Profile to switch to (see: verdict profile list)")):
    """Switch provider/model/key in one word - no secrets typed, ever."""
    config = load_config()
    if name not in config.profiles:
        _fail("use", f"no profile named '{name}' (saved: {', '.join(config.profiles) or 'none - see: verdict profile save'})")
    before = _masked_config(asdict(config))
    for k, v in config.profiles[name].items():
        setattr(config, k, v)
    save_config(config)
    audit.append("config_change", {"profile_applied": name, "before": before, "after": _masked_config(asdict(config))})
    ui.stage_ok("use", f"'{name}' active: {config.provider} / {config.model}")
    if config.provider != "ollama":
        ui.stage_warn("privacy", "cloud provider: diffs and intents WILL leave this machine")


scenario_app = typer.Typer(add_completion=False, help="Author scenarios without ever opening a YAML file.")
app.add_typer(scenario_app, name="scenario")

_SCENARIOS_FILE = Path(".verdict") / "scenarios" / "scenarios.yaml"


@scenario_app.command("add")
def scenario_add(
    name: str = typer.Option(None, help="short_snake_case name (prompted if omitted)"),
    description: str = typer.Option(None, help="One sentence: what must be true (prompted if omitted)"),
):
    """Add a scenario interactively - hand-authored scenarios are the
    highest-signal input in the system; this makes them a prompt, not a
    file format to learn."""
    from verdict.authoring import append_scenario

    if name is None:
        name = ui.console.input("  [bold cyan]scenario name[/] [dim](short_snake_case, e.g. limit_is_per_account)[/] > ").strip()
    if description is None:
        description = ui.console.input("  [bold cyan]what must be true?[/] [dim](one sentence, name the functions/values involved)[/] > ").strip()
    target = Path.cwd() / _SCENARIOS_FILE
    try:
        count = append_scenario(target, name, description)
    except AuthoringError as e:
        _fail("scenario", str(e))
    ui.stage_ok("scenario", f"'{name}' added - {count} scenario(s) in {_SCENARIOS_FILE}")
    ui.console.print(f"      [dim]run them:[/] [cyan]verdict run --scenarios {_SCENARIOS_FILE}[/]"
                     f"  [dim]or combined:[/] [cyan]verdict run --scenarios {_SCENARIOS_FILE} --hybrid[/]")


@scenario_app.command("list")
def scenario_list():
    """Show authored scenarios."""
    from verdict.authoring import load_scenarios

    target = Path.cwd() / _SCENARIOS_FILE
    try:
        gen = load_scenarios(target)
    except AuthoringError as e:
        ui.stage_warn("scenario", f"{e} - add one with: verdict scenario add")
        return
    ui.stage_ok("scenario", f"{len(gen.scenarios)} authored scenario(s) in {_SCENARIOS_FILE}")
    for s in gen.scenarios:
        ui.scenario_line(s.name, s.description)


@app.command("install-hook")
def install_hook():
    """Install the git pre-push hook: every push is verified before it leaves this machine."""
    try:
        path = hooks.install(Path.cwd())
    except hooks.HookError as e:
        _fail("hook", str(e))
    audit.append("hook_installed", {"path": str(path)})
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
    audit.append("hook_removed", {})
    ui.stage_ok("hook", "pre-push hook removed")


def _resolve_run_id(run_id: str) -> str:
    """'last' always means the newest run - nobody should have to copy ids around."""
    if run_id != "last":
        return run_id
    latest = latest_run_id()
    if latest is None:
        _fail("runs", "no runs recorded yet under .verdict/runs/")
    return latest


def _load_record(run_id: str) -> dict | None:
    """Single read path for every command that inspects a run. Prefers the
    Phase 2 database when configured (falls back to the file store), and
    attaches any overrides so OVERRIDDEN shows up everywhere a run does."""
    from verdict import store

    config = load_config()
    record = None
    url = store.resolve_database_url(config)
    if url:
        try:
            record = store.load_run_record(url, run_id)
            if record is not None:
                overrides = store.get_overrides(url, run_id)
                if overrides:
                    record["overrides"] = overrides
        except store.StoreError as e:
            ui.stage_warn("store", f"{e} - falling back to the file store")
    if record is None:
        record = load_run(run_id)
    return record


def _list_records(limit: int | None = None) -> list[dict]:
    from verdict import store

    config = load_config()
    url = store.resolve_database_url(config)
    if url:
        try:
            records = store.list_run_records(url, limit=limit)
            if records:
                return records
        except store.StoreError as e:
            ui.stage_warn("store", f"{e} - falling back to the file store")
    return list_runs(limit=limit)


@app.command()
def runs(limit: int = typer.Option(15, help="How many recent runs to show")):
    """Browse past verdicts as a table - the history without touching JSON."""
    records = _list_records(limit=limit)
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
    record = _load_record(_resolve_run_id(run_id))
    if record is None:
        _fail("report", f"no run named {run_id} under .verdict/runs/")
    path = save_html(record)
    ui.stage_ok("report", f"{path}")
    if open_browser:
        import webbrowser

        webbrowser.open(path.as_uri())


@app.command()
def status(run_id: str = typer.Argument("last", help="Run to check ('last' = newest)")):
    """One-line state of a run - queued/running in server mode, else its verdict."""
    record = _load_record(_resolve_run_id(run_id))
    if record is None:
        _fail("status", f"no run named {run_id}")
    state = record.get("status", "completed")
    if state == "completed":
        risk = record.get("risk") or {}
        detail = f"[bold]{risk.get('level', '?')}[/]  {risk.get('passed', 0)} passed / {risk.get('failed', 0)} failed"
    else:
        detail = f"[yellow]{state}[/] at stage '{record.get('failed_stage', record.get('stage', '?'))}'"
    if record.get("overrides"):
        detail += f"  [magenta]OVERRIDDEN ({len(record['overrides'])})[/]"
    ui.stage_ok("status", f"{record['run_id']}  {detail}")


@app.command()
def override(
    run_id: str = typer.Argument(..., help="Run to override"),
    reason: str = typer.Option(..., "--reason", help="Why this verdict is being overridden (required, logged)"),
):
    """Record a human override of a verdict - never edits the run, annotates it.

    Override rate is a first-class metric (Section 13): a tool that cries
    wolf gets disabled within a sprint, and this is the earliest signal."""
    from verdict import store

    if not reason.strip():
        _fail("override", "an override requires a real --reason - it is the audit trail for disagreeing with the verdict")
    config = load_config()
    url = store.resolve_database_url(config)
    if not url:
        _fail(
            "override",
            "overrides need the Phase 2 data layer - set database_url "
            "(verdict config set database_url postgresql://...) and run 'verdict db init'",
        )
    resolved = _resolve_run_id(run_id)
    try:
        # ensure the run exists in the DB even if it predates the data layer
        if store.load_run_record(url, resolved) is None:
            file_record = load_run(resolved)
            if file_record is None:
                _fail("override", f"no run named {resolved}")
            store.save_run_record(url, file_record)
        entry = store.add_override(url, resolved, reason.strip(), actor=_actor_name())
    except store.StoreError as e:
        _fail("override", str(e))
    audit.append("run_overridden", {"reason": reason.strip()}, run_id=resolved)
    ui.stage_ok("override", f"{resolved} overridden by {entry['actor']}")
    ui.console.print(f"      [dim]reason:[/] {reason.strip()}")
    try:
        rate = store.override_rate(url)
        if rate["override_rate"] is not None:
            ui.console.print(
                f"      [dim]override rate:[/] {rate['overridden_runs']}/{rate['completed_runs']} "
                f"completed runs ({rate['override_rate']:.1%}) [dim]- rising rate = earliest sign of a broken core[/]"
            )
    except store.StoreError:
        pass


def _actor_name() -> str:
    import getpass

    try:
        return f"user:{getpass.getuser()}"
    except OSError:
        return "user:unknown"


db_app = typer.Typer(add_completion=False, help="Phase 2 data layer: Postgres setup and migration.")
app.add_typer(db_app, name="db")


@db_app.command("init")
def db_init():
    """Create the Postgres schema (idempotent - safe to re-run)."""
    from verdict import store

    config = load_config()
    url = store.resolve_database_url(config)
    if not url:
        _fail("db", "no database_url configured - verdict config set database_url postgresql://user:pass@host:5432/verdict")
    try:
        store.init_schema(url)
    except store.StoreError as e:
        _fail("db", str(e))
    ui.stage_ok("db", "schema ready (runs, results, audit_log, overrides, jobs)")


@db_app.command("migrate-files")
def db_migrate_files():
    """Backfill existing .verdict/runs/*.json and audit.jsonl into Postgres."""
    from verdict import store

    config = load_config()
    url = store.resolve_database_url(config)
    if not url:
        _fail("db", "no database_url configured")
    try:
        store.init_schema(url)
        counts = store.migrate_files(url)
    except store.StoreError as e:
        _fail("db", str(e))
    audit.append("db_migrated", counts)
    ui.stage_ok("db", f"migrated {counts['runs']} run(s), {counts['audit_entries']} new audit entrie(s)")


@db_app.command("stats")
def db_stats():
    """Override rate and run counts - the Section 13 first-class metric."""
    from verdict import store

    config = load_config()
    url = store.resolve_database_url(config)
    if not url:
        _fail("db", "no database_url configured")
    try:
        rate = store.override_rate(url)
    except store.StoreError as e:
        _fail("db", str(e))
    ui.stage_ok("db", f"{rate['completed_runs']} completed run(s), {rate['overridden_runs']} overridden")
    if rate["override_rate"] is not None:
        ui.console.print(f"      [dim]override rate:[/] {rate['override_rate']:.1%}")


@app.command()
def logs(run_id: str = typer.Argument("last", help="Run to inspect ('last' = newest)")):
    """Full evidence for a run: prompt, test code, sandbox output - the audit trail."""
    record = _load_record(_resolve_run_id(run_id))
    if record is None:
        _fail("logs", f"no run named {run_id} under .verdict/runs/")
    ui.console.print(f"[bold cyan]run[/]     {record['run_id']}  [dim]({record['created_at']})[/]")
    ui.console.print(f"[bold cyan]model[/]   {record['model']}")
    if record.get("scope"):
        ui.console.print(f"[bold cyan]checked[/] {record['scope']}")
    intent_txt = record.get("intent") or "(never extracted)"
    ui.console.print(f"[bold cyan]intent[/]  {intent_txt.splitlines()[0]}")
    for ov in record.get("overrides", []):
        ui.console.print(f"[bold magenta]override[/] by {ov['actor']} at {ov['created_at']}: {ov['reason']}")
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
