"""
Rich-based presentation layer for the CLI. Presentation only - every piece
of logic stays in the pipeline modules. --json output bypasses all of this.
"""
import sys

from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

console = Console(highlight=False)


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


def banner(mode: str, model: str) -> None:
    title = Text()
    title.append("  VERDICT", style="bold cyan")
    title.append("  proof, not vibes", style="dim italic")
    console.print(title)
    console.print(f"  [dim]model[/] [cyan]{model}[/]  [dim]mode[/] [cyan]{mode}[/]\n")


def shell_banner(model: str, ollama_ok: bool, docker_ok: bool) -> None:
    console.print(f"[bold cyan]{WORDMARK}[/]")
    console.print("  [dim italic]proof, not vibes - the neutral referee for AI-written code[/]\n")
    ollama_txt = "[green]ready[/]" if ollama_ok else "[red]down[/]"
    docker_txt = "[green]ready[/]" if docker_ok else "[red]down[/]"
    console.print(f"  [dim]model[/] [cyan]{model}[/]   [dim]ollama[/] {ollama_txt}   [dim]docker[/] {docker_txt}")
    console.print("  [dim]type[/] [cyan]help[/] [dim]for commands,[/] [cyan]exit[/] [dim]to leave[/]\n")


def shell_help() -> None:
    table = Table.grid(padding=(0, 3))
    table.add_column(style="cyan", justify="left")
    table.add_column(style="dim")
    table.add_row("run [options]", "verify a change (--ref, --base, --intent, --scenarios, --max-scenarios)")
    table.add_row("plan [options]", "dry-run: show scenarios without executing (--manual writes a template)")
    table.add_row("logs <run-id>", "full evidence for a past run")
    table.add_row("health", "liveness check: config, Ollama, Docker")
    table.add_row("init [options]", "first-time setup for this repo")
    table.add_row("clear", "clear the screen")
    table.add_row("exit / quit", "leave the verdict shell")
    console.print(table)
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
    headline = Text()
    headline.append(f" {level} RISK ", style=style)
    if coverage is not None:
        headline.append(f"  {risk['passed']}/{conclusive} conclusive passed", style="bold")
        headline.append(f"  {DOT}  coverage {coverage:.0%}", style="dim")
    else:
        headline.append("  no conclusive evidence - human review required", style="bold yellow")

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
    tokens = record.get("tokens") or {}
    if tokens.get("llm_calls"):
        footer += (
            f"\n  [dim]llm[/] {tokens['llm_calls']} call(s)"
            f"   [dim]tokens[/] {tokens['prompt_tokens']:,} in / {tokens['output_tokens']:,} out"
            f"   [dim]llm time[/] {tokens['llm_seconds']}s"
        )
    console.print(footer + "\n")


def show_test_code(code: str) -> None:
    console.print(Syntax(code, "python", theme="monokai", line_numbers=True, word_wrap=True))
