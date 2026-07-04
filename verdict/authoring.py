"""
Module 3b - Scenario Authoring (manual).

Input:  developer-written YAML or JSON scenario file
Output: GenerationResult with source="manual" - same schema as Module 3a,
        so everything downstream (validator, sandbox, scorer) is identical
        regardless of who authored the scenarios.
"""
import json
from pathlib import Path

import yaml

from verdict.generator import GenerationResult, Scenario

TEMPLATE = """\
# Verdict scenario file - edit this, then run:
#   verdict run --scenarios <this file>
#
# Each scenario needs a name (short_snake_case) and a one-sentence
# description of the behavior being verified.
intent: "{intent}"
scenarios:
  - name: example_behavior_holds
    description: "Describe the specific behavior this change must exhibit."
  - name: example_edge_case
    description: "Describe an edge case that must not break."
"""


class AuthoringError(Exception):
    pass


def validate_scenario_name(name: str) -> str | None:
    """None if `name` is a valid short_snake_case scenario name, else the
    reason it isn't - shared so the interactive `scenario add` prompt can
    validate the name the moment it's typed, before asking for anything else."""
    name = name.strip()
    if not name:
        return "a scenario needs a name"
    if not all(c.isalnum() or c == "_" for c in name):
        return f"scenario name '{name}' must be short_snake_case (letters, digits, underscores)"
    return None


def write_template(path: Path, intent: str = "") -> Path:
    if path.exists():
        raise AuthoringError(f"{path} already exists - not overwriting your scenarios")
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_intent = intent.replace('"', "'").splitlines()[0] if intent else ""
    path.write_text(TEMPLATE.format(intent=safe_intent), encoding="utf-8")
    return path


def append_scenario(path: Path, name: str, description: str, intent: str = "") -> int:
    """Add one scenario to a file without the user ever opening it - the
    `verdict scenario add` backend. Creates the file if missing. Returns the
    new scenario count.

    Note: an existing file is parsed and re-dumped, so hand-written comments
    don't survive - by design, the whole point of `scenario add` is that
    nobody hand-edits this file."""
    name = name.strip()
    description = description.strip()
    if (reason := validate_scenario_name(name)) is not None:
        raise AuthoringError(reason)
    if not description:
        raise AuthoringError("a scenario needs a description")

    if path.exists():
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            raise AuthoringError(f"could not parse existing {path.name}: {e}") from e
        if not isinstance(data, dict):
            raise AuthoringError(f"{path.name} is not a scenario file")
    else:
        # No placeholder value written when there's nothing real to put here -
        # an empty/stale intent line is exactly the kind of vague-placeholder
        # confusion the tool itself flags elsewhere (check_vagueness).
        data = {}
        if intent:
            data["intent"] = intent.replace('"', "'").splitlines()[0]
    scenarios = data.setdefault("scenarios", [])
    if not isinstance(scenarios, list):
        raise AuthoringError(f"{path.name} has a non-list 'scenarios' key")
    # drop unedited template placeholders the moment a real scenario arrives
    scenarios[:] = [s for s in scenarios if not str((s or {}).get("name", "")).startswith("example_")]
    if any(str((s or {}).get("name", "")) == name for s in scenarios):
        raise AuthoringError(f"a scenario named '{name}' already exists in {path.name}")
    scenarios.append({"name": name, "description": description})

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Verdict scenario file - managed by `verdict scenario add`; run with:\n"
        "#   verdict run --scenarios <this file>   (or --hybrid to combine with generated ones)\n"
        + yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return len(scenarios)


def load_scenarios(path: Path) -> GenerationResult:
    if not path.exists():
        raise AuthoringError(f"scenario file not found: {path}")

    text = path.read_text(encoding="utf-8")
    try:
        if path.suffix.lower() == ".json":
            data = json.loads(text)
        else:
            data = yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as e:
        raise AuthoringError(f"could not parse {path.name}: {e}") from e

    if not isinstance(data, dict) or not isinstance(data.get("scenarios"), list):
        raise AuthoringError(f"{path.name} must contain a top-level 'scenarios' list")

    scenarios = []
    for i, item in enumerate(data["scenarios"], 1):
        if not isinstance(item, dict):
            raise AuthoringError(f"scenario #{i} is not a mapping with name/description")
        name = str(item.get("name", "")).strip()
        description = str(item.get("description", "")).strip()
        if not name or not description:
            raise AuthoringError(f"scenario #{i} is missing a name or description")
        if name.startswith("example_"):
            raise AuthoringError(
                f"scenario '{name}' is still the unedited template placeholder - "
                "replace it with a real scenario"
            )
        scenarios.append(Scenario(name=name, description=description))

    if not scenarios:
        raise AuthoringError(f"{path.name} contains no scenarios")

    return GenerationResult(
        scenarios=scenarios,
        model="",
        prompt="",
        raw_response=text,
        source="manual",
    )
