"""Phase 0 - retry entries that failed during generate_scenarios.py (e.g. server dropped mid-batch)."""
import json
from pathlib import Path

from generate_scenarios import PROMPT_TEMPLATE, call_ollama

SCENARIOS_FILE = Path(__file__).parent / "scenarios.json"


def main():
    results = json.loads(SCENARIOS_FILE.read_text(encoding="utf-8"))
    retried = 0
    for entry in results:
        if entry["scenarios"]:
            continue
        print(f"Retrying {entry['id']} - {entry['intent'][:60]}")
        prompt = PROMPT_TEMPLATE.format(intent=entry["intent"], diff=entry["diff"])
        try:
            parsed = call_ollama(prompt)
            entry["scenarios"] = parsed.get("scenarios", [])
            print(f"  -> {len(entry['scenarios'])} scenarios generated")
            retried += 1
        except Exception as e:
            print(f"  ERROR: {e}")

    SCENARIOS_FILE.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nRetried {retried} entries. Updated {SCENARIOS_FILE}")


if __name__ == "__main__":
    main()
