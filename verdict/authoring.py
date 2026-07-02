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


def write_template(path: Path, intent: str = "") -> Path:
    if path.exists():
        raise AuthoringError(f"{path} already exists - not overwriting your scenarios")
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_intent = intent.replace('"', "'").splitlines()[0] if intent else ""
    path.write_text(TEMPLATE.format(intent=safe_intent), encoding="utf-8")
    return path


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
