"""
Phase 0 - Step 2: for each {diff, intent} pair, call the local Ollama model
(qwen2.5-coder:7b) and ask it to propose test scenarios. This is the one
narrow LLM step per the doc (Module 3a) - everything else stays deterministic.
"""
import json
import urllib.request
from pathlib import Path

DATASET_FILE = Path(__file__).parent / "dataset.json"
OUT_FILE = Path(__file__).parent / "scenarios.json"
OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "qwen2.5-coder:7b"

PROMPT_TEMPLATE = """You are reviewing a code change to verify it does what it claims.

Stated intent (from the commit message):
{intent}

Diff:
{diff}

Propose 2-5 concrete test scenarios that would verify whether this change
actually fulfills the stated intent. Each scenario must be something you
could plausibly check by running code against this diff - not a vague
suggestion. Respond with ONLY valid JSON in this exact shape, no other text:

{{"scenarios": [{{"name": "short_snake_case_name", "description": "one sentence describing what is being verified"}}]}}
"""


def call_ollama(prompt: str) -> dict:
    payload = json.dumps(
        {
            "model": MODEL,
            "prompt": prompt,
            "format": "json",
            "stream": False,
            "options": {"temperature": 0.2},
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_URL, data=payload, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return json.loads(body["response"])


def main():
    dataset = json.loads(DATASET_FILE.read_text(encoding="utf-8"))
    results = []
    for i, entry in enumerate(dataset, 1):
        print(f"[{i}/{len(dataset)}] {entry['id']} - {entry['intent'][:60]}")
        prompt = PROMPT_TEMPLATE.format(intent=entry["intent"], diff=entry["diff"])
        try:
            parsed = call_ollama(prompt)
            scenarios = parsed.get("scenarios", [])
        except Exception as e:
            print(f"  ERROR: {e}")
            scenarios = []
        print(f"  -> {len(scenarios)} scenarios generated")
        results.append(
            {
                "id": entry["id"],
                "intent": entry["intent"],
                "diff": entry["diff"],
                "scenarios": scenarios,
            }
        )

    OUT_FILE.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nWrote results for {len(results)} entries to {OUT_FILE}")


if __name__ == "__main__":
    main()
