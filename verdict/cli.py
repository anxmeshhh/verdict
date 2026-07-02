"""Module 8 - CLI. Wires the pipeline modules together as `verdict <command>`."""
import typer

from verdict.config import Config, check_ollama, is_initialized, load_config, save_config

app = typer.Typer(add_completion=False, help="Verdict - proves code does what it claims, before a human reviews it.")


@app.command()
def init(
    model: str = typer.Option(None, help="Ollama model to use (defaults to qwen2.5-coder:7b)"),
    ollama_url: str = typer.Option(None, help="Ollama server URL (defaults to http://localhost:11434)"),
):
    """Module 1: one-time setup - writes .verdict/config.json and checks Ollama is ready."""
    existing = load_config()
    config = Config(
        model=model or existing.model,
        ollama_url=ollama_url or existing.ollama_url,
    )
    path = save_config(config)
    typer.echo(f"[1/2] config   OK   {path} (model: {config.model})")

    status = check_ollama(config.ollama_url)
    if not status.reachable:
        typer.echo(f"[2/2] ollama   FAIL not reachable at {config.ollama_url} - is 'ollama serve' running?")
        raise typer.Exit(code=1)

    if config.model not in status.models:
        typer.echo(f"[2/2] ollama   WARN reachable, but model '{config.model}' not pulled yet.")
        typer.echo(f"             Run: ollama pull {config.model}")
        raise typer.Exit(code=1)

    typer.echo(f"[2/2] ollama   OK   reachable at {config.ollama_url}, model '{config.model}' ready")


@app.command()
def health():
    """Quick liveness check - same Ollama check as init, without touching config."""
    config = load_config()
    if not is_initialized():
        typer.echo("config    WARN no .verdict/config.json - run 'verdict init' first")
    else:
        typer.echo(f"config    OK   model: {config.model}")

    status = check_ollama(config.ollama_url)
    if not status.reachable:
        typer.echo(f"ollama    FAIL not reachable at {config.ollama_url}")
        raise typer.Exit(code=1)
    if config.model not in status.models:
        typer.echo(f"ollama    WARN reachable, but model '{config.model}' not pulled")
        raise typer.Exit(code=1)
    typer.echo(f"ollama    OK   {config.model} loaded, {len(status.models)} model(s) available")


if __name__ == "__main__":
    app()
