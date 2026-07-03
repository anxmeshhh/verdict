"""
Module 1 - Config & Setup.

Input:  none (first run)
Output: local .verdict/config.json + a live check that Ollama is reachable
        and the configured model is actually pulled.

Phase 1 scope only: no DB/Redis here yet (Section 12, Phase 1 = CLI only).
"""
import json
import urllib.request
import urllib.error
from dataclasses import dataclass, asdict
from pathlib import Path

CONFIG_DIRNAME = ".verdict"
CONFIG_FILENAME = "config.json"

DEFAULT_MODEL = "qwen2.5-coder:7b"
DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_PROVIDER = "ollama"


@dataclass
class Config:
    model: str = DEFAULT_MODEL
    ollama_url: str = DEFAULT_OLLAMA_URL
    # "ollama" (local, default) or an API provider: openrouter | groq | gemini | openai | custom
    provider: str = DEFAULT_PROVIDER
    api_key: str = ""  # for API providers; VERDICT_API_KEY env var takes precedence
    base_url: str = ""  # override endpoint (required for provider=custom, e.g. a vLLM server)
    # Phase 2 data layer: when set, runs/audit dual-write to Postgres and read
    # commands prefer it. Empty = file store only (the Phase 1 default).
    # VERDICT_DATABASE_URL env var takes precedence.
    database_url: str = ""


def config_dir(root: Path | None = None) -> Path:
    return (root or Path.cwd()) / CONFIG_DIRNAME


def config_path(root: Path | None = None) -> Path:
    return config_dir(root) / CONFIG_FILENAME


def is_initialized(root: Path | None = None) -> bool:
    return config_path(root).exists()


def load_config(root: Path | None = None) -> Config:
    path = config_path(root)
    if not path.exists():
        return Config()
    data = json.loads(path.read_text(encoding="utf-8"))
    known = asdict(Config())
    merged = {**known, **{k: v for k, v in data.items() if k in known}}
    return Config(**merged)


def save_config(config: Config, root: Path | None = None) -> Path:
    path = config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(config), indent=2), encoding="utf-8")
    return path


_GITIGNORE_MARKER = "# added by verdict init - .verdict/ holds full diffs, prompts, and raw LLM"
_GITIGNORE_BLOCK = f"""
{_GITIGNORE_MARKER}
# responses in plaintext (cache/, runs/, audit.jsonl) - never commit it
.verdict/
"""
_ALREADY_COVERED = {".verdict/", ".verdict", "/.verdict/", "/.verdict"}


def ensure_gitignore(root: Path | None = None) -> Path | None:
    """`.verdict/` holds full diffs, raw prompts/responses, and generated test
    code in plaintext (cache/, runs/, audit.jsonl) - a plain `git add -A` in a
    repo with no .gitignore entry for it stages all of that straight into
    version control, silently defeating the same leak --json's clean output
    was designed to prevent. Called from `verdict init` so this is closed by
    default, not an opt-in a user has to know to set up themselves.

    Returns the .gitignore path if it created/modified one, None if `.verdict/`
    was already covered (no action taken, existing file untouched)."""
    repo = root or Path.cwd()
    path = repo / ".gitignore"
    if path.exists():
        existing = path.read_text(encoding="utf-8", errors="replace")
        if any(line.strip() in _ALREADY_COVERED for line in existing.splitlines()):
            return None
        separator = "" if existing.endswith("\n") else "\n"
        path.write_text(existing + separator + _GITIGNORE_BLOCK, encoding="utf-8")
        return path
    path.write_text(_GITIGNORE_BLOCK.lstrip("\n"), encoding="utf-8")
    return path


@dataclass
class OllamaStatus:
    reachable: bool
    models: list[str]
    error: str | None = None


def check_ollama(ollama_url: str, timeout: float = 5.0) -> OllamaStatus:
    """Live check - never assume, never cache a stale answer (Section 11: degrade honestly)."""
    try:
        with urllib.request.urlopen(f"{ollama_url}/api/tags", timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        models = [m["name"] for m in body.get("models", [])]
        return OllamaStatus(reachable=True, models=models)
    except (urllib.error.URLError, OSError, ValueError) as e:
        return OllamaStatus(reachable=False, models=[], error=str(e))
