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


@dataclass
class Config:
    model: str = DEFAULT_MODEL
    ollama_url: str = DEFAULT_OLLAMA_URL


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
    return Config(**{**asdict(Config()), **data})


def save_config(config: Config, root: Path | None = None) -> Path:
    path = config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(config), indent=2), encoding="utf-8")
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
