"""
Rich-based presentation layer for the CLI. Presentation only - every piece
of logic stays in the pipeline modules.

In --json mode, the FINAL json blob is the only thing allowed on stdout -
anything else there breaks every consumer that pipes stdout into a JSON
parser (CI, jq, a dashboard). route_to_stderr() moves all of this module's
progress/status output to stderr instead, the standard stdout=result /
stderr=human-log convention - a human watching a --json run in a terminal
sees no difference, since terminals interleave both streams by default.
"""
import sys

from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

console = Console(highlight=False)


def route_to_stderr() -> None:
    """Call once, before any other ui.* output, whenever --json is active."""
    global console
    console = Console(stderr=True, highlight=False)


def _pick(fancy: str, plain: str) -> str:
    """Unicode glyphs only on a real terminal whose encoding can print them;
    piped/redirected output always gets plain ASCII."""
    if not sys.stdout.isatty():
        return plain
    try:
        fancy.encode(sys.stdout.encoding or "utf-8")
        return fancy
    except (UnicodeEncodeError, LookupError):
        return plain


CHECK = _pick("✓", "+")
CROSS = _pick("✗", "x")
WARN_MARK = _pick("!", "!")
DOT = _pick("·", "-")
ARROW = _pick("›", ">")
RULE = _pick("═══", "===")

RISK_STYLES = {
    "LOW": "bold white on green",
    "MEDIUM": "bold black on yellow",
    "HIGH": "bold white on red",
    "UNVERIFIED": "bold white on grey35",
}

STATUS_STYLES = {
    "passed": ("PASSED", "bold green"),
    "failed": ("FAILED", "bold red"),
    "uncertain": ("UNCLEAR", "yellow"),
    "error": ("BAD TEST", "dim yellow"),
    "timeout": ("TIMEOUT", "magenta"),
}


WORDMARK = r"""
 __   _____ ___ ___ ___  ___ _____
 \ \ / / __| _ \   \_ _|/ __|_   _|
  \ V /| _||   / |) | || (__  | |
   \_/ |___|_|_\___/___|\___| |_|
"""


def banner(mode: str, model: str, provider: str = "ollama") -> None:
    title = Text()
    title.append("  VERDICT", style="bold cyan")
    title.append("  proof, not vibes", style="dim italic")
    console.print(title)
    where = "[dim](local)[/]" if provider == "ollama" else f"[yellow]@ {provider}[/]"
    console.print(f"  [dim]model[/] [cyan]{model}[/] {where}  [dim]mode[/] [cyan]{mode}[/]\n")


def first_run_banner() -> None:
    """Shown instead of shell_banner() when this repo has never run `init` -
    a fresh Config() defaults to ollama/qwen2.5-coder:7b, so checking
    liveness before setup would just show a scary, misleading 'down' for a
    provider the user hasn't chosen yet. Point at the one real next step."""
    console.print(f"[bold cyan]{WORDMARK}[/]")
    console.print("  [dim italic]proof, not vibes - the neutral referee for AI-written code[/]\n")
    console.print(
        "  Looks like this is your first time here. Verdict proves a code change\n"
        "  does what it claims - it needs an LLM (free & local via Ollama, or your\n"
        "  own cloud API key) and Docker to run generated tests in.\n"
    )
    console.print("  [bold cyan]init[/]        [dim]one-time setup - pick local or cloud right here[/]")
    console.print("  [dim]e.g.[/] [cyan]init --provider groq --model llama-3.3-70b-versatile --api-key <key>[/]\n")
    console.print("  [dim]type[/] [cyan]help[/] [dim]to see everything else,[/] [cyan]exit[/] [dim]to leave[/]\n")


def shell_banner(model: str, provider: str, llm_ok: bool, docker_ok: bool) -> None:
    console.print(f"[bold cyan]{WORDMARK}[/]")
    console.print("  [dim italic]proof, not vibes - the neutral referee for AI-written code[/]\n")
    llm_txt = "[green]ready[/]" if llm_ok else "[red]down[/]"
    docker_txt = "[green]ready[/]" if docker_ok else "[red]down[/]"
    local_tag = "" if provider == "ollama" else " [yellow](cloud)[/]"
    console.print(f"  [dim]model[/] [cyan]{model}[/]   [dim]{provider}[/]{local_tag} {llm_txt}   [dim]docker[/] {docker_txt}")
    console.print("  [dim]type[/] [cyan]help[/] [dim]for commands,[/] [cyan]exit[/] [dim]to leave[/]\n")


def _command_table(rows: list[tuple[str, str]]) -> Table:
    table = Table.grid(padding=(0, 3))
    table.add_column(style="cyan", justify="left")
    table.add_column(style="dim")
    for name, detail in rows:
        table.add_row(name, detail)
    return table


def shell_help() -> None:
    console.print("[bold]Getting started[/]")
    console.print(_command_table([
        ("init [options]", "first-time setup for this repo"),
        ("model", "pick a provider + model interactively - API key shown in plain text so paste works"),
        ("health", "liveness check: config, LLM provider, Docker, and (if configured) Postgres/Redis/queue"),
        ("run [options]", "verify a change (--ref, --base, --intent, --scenarios, --hybrid, --force-regenerate)"),
        ("check", "verify the obvious thing - no flags (uncommitted changes, else last commit)"),
    ]))
    console.print("\n[bold]Everyday[/]")
    console.print(_command_table([
        ("plan [options]", "dry-run: show scenarios without executing (--manual writes a template)"),
        ("watch [options]", "live mode: verify automatically when the working tree settles"),
        ("use <profile>", "switch provider by name, no secrets typed (see: profile save/list)"),
        ("runs", "browse past verdicts as a table"),
        ("report [run-id]", "export a run as a shareable HTML page ('last' = newest)"),
        ("logs [run-id]", "full evidence for a past run ('last' = newest)"),
        ("status [run-id]", "one-line state of a run - queued/running in server mode, else its verdict"),
        ("scenario add", "author a scenario interactively - highest-signal input, no YAML to learn"),
        ("scenario list", "show saved scenarios for this repo"),
    ]))
    console.print("\n[bold]Admin, server & CI[/]")
    console.print(_command_table([
        ("config get/set", "settings: model, provider (ollama/openrouter/groq/gemini/...), api_key"),
        ("profile save/list/delete", "manage named provider profiles (see: use <profile>)"),
        ("override <run-id>", "record a human override of a verdict (--reason required)"),
        ("db init/migrate-files/stats", "Phase 2 data layer: Postgres schema, file-store backfill, stats"),
        ("serve", "launch the API gateway (Phase 3 server mode)"),
        ("worker", "launch a queue worker that executes runs the API enqueues"),
        ("install-hook", "pre-push gate: verify every push before it leaves this machine"),
        ("uninstall-hook", "remove the verdict pre-push hook"),
    ]))
    console.print("\n[bold]Shell[/]")
    console.print(_command_table([
        ("clear", "clear the screen"),
        ("exit / quit", "leave the verdict shell"),
    ]))
    console.print()


def stage_ok(name: str, detail: str = "") -> None:
    console.print(f"  [green]{CHECK}[/] [bold]{name:14}[/] [white]{detail}[/]")


def stage_note(name: str, detail: str) -> None:
    console.print(f"  [dim]{DOT}[/] [bold dim]{name:14}[/] [dim]{detail}[/]")


def stage_warn(name: str, detail: str) -> None:
    console.print(f"  [yellow]![/] [bold]{name:14}[/] [yellow]{detail}[/]")


def stage_fail(name: str, detail: str) -> None:
    console.print(f"  [red]{CROSS}[/] [bold]{name:14}[/] [red]{detail}[/]")


def scenario_line(name: str, description: str = "") -> None:
    desc = f" [dim]{description[:70]}[/]" if description else ""
    console.print(f"      [cyan]{ARROW}[/] [cyan]{name}[/]{desc}")


def working(message: str):
    """Spinner context manager for long operations (LLM calls, containers)."""
    return console.status(f"[cyan]{message}[/]", spinner="dots")


def result_line(name: str, status: str, duration_s: float) -> None:
    tag, style = STATUS_STYLES.get(status, (status.upper(), "white"))
    console.print(f"      [{style}]{tag:8}[/] {name} [dim]({duration_s}s)[/]")


def verdict_panel(record: dict) -> None:
    risk = record["risk"]
    level = risk["level"]
    style = RISK_STYLES.get(level, "bold")

    table = Table.grid(padding=(0, 2))
    table.add_column(justify="left")
    table.add_column(justify="left")
    for r in record["results"]:
        tag, tag_style = STATUS_STYLES.get(r["status"], (r["status"].upper(), "white"))
        first_line = (r["stdout"].strip().splitlines() or [""])[0]
        table.add_row(
            Text(tag, style=tag_style),
            Text(f"{r['scenario_name']}  ", style="bold") + Text(f"({r['duration_s']}s)", style="dim"),
        )
        if first_line:
            table.add_row("", Text(first_line[:90], style="dim italic"))

    coverage = risk["coverage"]
    conclusive = risk["passed"] + risk["failed"]
    inconclusive = risk.get("inconclusive", 0)
    # Two numbers, always - "executed of planned" - so nobody has to trust
    # that nothing was quietly excluded; they can see the counts disagree.
    executed = len(record.get("results") or [])
    planned = executed + len(record.get("ungeneratable") or []) + len(record.get("scenario_cap_dropped") or [])
    headline = Text()
    headline.append(f" {level} RISK ", style=style)
    if coverage is not None:
        headline.append(f"  {risk['passed']}/{conclusive} conclusive passed", style="bold")
        headline.append(f"  {DOT}  coverage {coverage:.0%}", style="dim")
        headline.append(
            f"  {DOT}  {executed}/{planned} planned scenario(s) executed",
            style="dim" if executed == planned else "bold yellow",
        )
        # Coverage is deliberately computed over conclusive results only
        # (uncertain/error/timeout are non-evidence, not a strike against the
        # change) - but that means a scenario the model couldn't check at all
        # is invisible in "100% coverage" unless called out right here too,
        # not just buried in the reasons list below.
        if inconclusive:
            headline.append(
                f"  {DOT}  {inconclusive} scenario(s) produced no evidence (excluded from coverage)",
                style="bold yellow",
            )
    else:
        headline.append("  no conclusive evidence - human review required", style="bold yellow")
    cap_dropped = record.get("scenario_cap_dropped") or []
    if cap_dropped:
        # A capped scenario is a different problem from an inconclusive one:
        # it was never even attempted, so it's invisible to passed/failed/
        # coverage math entirely - "traceable" and "conclusive passed" can
        # both look complete while real, validated coverage was silently cut.
        headline.append(
            f"  {DOT}  {len(cap_dropped)} validated scenario(s) NOT run at all (--max-scenarios cap)",
            style="bold red",
        )

    body = Table.grid(padding=(0, 0))
    body.add_row(headline)
    body.add_row(Text(""))
    body.add_row(table)
    reasons = Text("\n".join(f"  {reason}" for reason in risk["reasons"]), style="dim")
    body.add_row(reasons)

    console.print()
    console.print(Panel(body, title="[bold cyan]VERDICT[/]", border_style="cyan", padding=(1, 2)))
    footer = (
        f"  [dim]run[/] [bold]{record['run_id']}[/]"
        f"   [dim]full evidence:[/] [cyan]verdict logs {record['run_id']}[/]"
    )
    if record.get("scope"):
        footer = f"  [dim]checked[/] {record['scope']}   [dim]model[/] {record.get('model', '?')}\n" + footer
    tokens = record.get("tokens") or {}
    if tokens.get("llm_calls"):
        footer += (
            f"\n  [dim]llm[/] {tokens['llm_calls']} call(s)"
            f"   [dim]tokens[/] {tokens['prompt_tokens']:,} in / {tokens['output_tokens']:,} out"
            f"   [dim]llm time[/] {tokens['llm_seconds']}s"
        )
    console.print(footer + "\n")


def runs_table(records: list[dict]) -> None:
    """Past verdicts as a table - history at a glance, JSON stays on disk."""
    table = Table(border_style="dim", header_style="bold cyan", padding=(0, 1))
    table.add_column("run")
    table.add_column("when", style="dim")
    table.add_column("verdict")
    table.add_column("evidence", style="dim")
    table.add_column("intent")
    table.add_column("llm", style="dim", justify="right")
    for record in records:
        status = record.get("status", "completed")
        if status == "completed":
            risk = record.get("risk") or {}
            level = risk.get("level", "?")
            verdict = Text(f" {level} ", style=RISK_STYLES.get(level, "bold"))
            if record.get("overrides"):
                verdict.append(" OVERRIDDEN ", style="bold magenta")
            evidence = f"{risk.get('passed', 0)} passed / {risk.get('failed', 0)} failed"
            if risk.get("inconclusive"):
                evidence += f" / {risk['inconclusive']} no-evidence"
            if record.get("scenario_cap_dropped"):
                evidence += f" / {len(record['scenario_cap_dropped'])} not run (cap)"
        else:
            verdict = Text(f" {status.upper()} ", style="bold yellow" if status == "errored" else "dim")
            evidence = f"at {record.get('failed_stage', '?')}"
        when = (record.get("created_at") or "")[:16].replace("T", " ")
        intent = (record.get("intent") or "-").splitlines()[0][:44]
        tokens = record.get("tokens") or {}
        llm = f"{tokens.get('prompt_tokens', 0) + tokens.get('output_tokens', 0):,} tok" if tokens.get("llm_calls") else "-"
        table.add_row(record["run_id"], when, verdict, evidence, intent, llm)
    console.print(table)


SEVERITY_STYLES = {
    "CRITICAL": "bold white on red",
    "HIGH": "bold white on red",
    "MEDIUM": "bold black on yellow",
    "LOW": "bold white on green",
}


def findings_table(findings: list[dict]) -> None:
    """Verdict Intelligence's findings as a table - the CLI-first view of the
    standing vulnerability map, matching verdict runs. Shows what the four
    agents did to each finding (correlation, re-verify flag, suggested fix)
    without anyone opening the web page or reading JSON."""
    table = Table(border_style="dim", header_style="bold cyan", padding=(0, 1))
    table.add_column("id", style="dim", justify="right")
    table.add_column("repo")
    table.add_column("class")
    table.add_column("severity")
    table.add_column("status", style="dim")
    table.add_column("agents")
    table.add_column("suggested fix", style="dim")
    for f in findings:
        sev = (f.get("severity") or "").upper()
        severity = Text(f" {sev} ", style=SEVERITY_STYLES.get(sev, "dim")) if sev else Text("-", style="dim")
        agents = Text()
        if f.get("correlated_with"):
            agents.append(f"recurrence of #{f['correlated_with']}", style="yellow")
        if f.get("reverification_reason"):
            agents.append("  re-verify", style="bold yellow")
        fix = f.get("suggested_fix") or ""
        fix_short = (fix[:50] + "...") if len(fix) > 50 else fix
        table.add_row(
            str(f.get("id", "")),
            f.get("repo_name") or "-",
            f.get("vuln_class") or "-",
            severity,
            f.get("status") or "open",
            agents or Text("-", style="dim"),
            fix_short or "-",
        )
    console.print(table)


def show_test_code(code: str) -> None:
    console.print(Syntax(code, "python", theme="monokai", line_numbers=True, word_wrap=True))
