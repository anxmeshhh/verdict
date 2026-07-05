"""
Phase 6 precision/recall gate - same methodology as phase0/'s original
precision test: real {diff, intent} pairs go through the real scenario-gen
LLM call and the real validator, and a human-readable judgment gets recorded
per example. No sandbox execution here (same scope Phase 0 used) - this
measures whether the detection layer (generator + validator) surfaces the
right vuln_class for a real, known vulnerability, not whether a full
container-based test run would catch it too.

A commit counts as a TRUE POSITIVE only if a scenario tagged with the
CORRECT vuln_class survived the validator (i.e. was judged traceable to the
actual diff) - a scenario that's merely proposed but rejected as untraceable,
or tagged with the wrong class, does not count.
"""
import json
import time
from dataclasses import asdict
from pathlib import Path

from verdict.config import load_config
from verdict.generator import GenerationError, generate
from verdict.intent import IntentResult
from verdict.validator import validate

DATASET_PATH = Path(__file__).parent / "dataset.json"
RESULTS_PATH = Path(__file__).parent / "gate_results.json"
CACHE_REPO = Path(__file__).parent  # scenario-gen cache lives under here, not the real Verdict repo


def evaluate() -> dict:
    config = load_config(Path.cwd())
    examples = json.loads(DATASET_PATH.read_text(encoding="utf-8"))

    per_class = {}
    details = []

    for ex in examples:
        vclass = ex["vuln_class"]
        per_class.setdefault(vclass, {"total": 0, "caught": 0})
        per_class[vclass]["total"] += 1

        intent_result = IntentResult(diff=ex["diff"], intent=ex["intent"], vague=False)
        entry = {
            "hash": ex["hash"][:12],
            "repo_url": ex["repo_url"],
            "expected_vuln_class": vclass,
        }
        try:
            generation = generate(intent_result, config, repo=CACHE_REPO, force=True)
        except GenerationError as e:
            entry["outcome"] = "generation_error"
            entry["detail"] = str(e)
            details.append(entry)
            continue

        validations = validate(generation.scenarios, intent_result.diff, intent_result.intent)
        kept = [v.scenario for v in validations if v.traceable]
        tagged_correct = [s for s in kept if s.vuln_class == vclass]
        all_proposed_classes = [s.vuln_class for s in generation.scenarios if s.vuln_class]

        entry["proposed_vuln_classes"] = all_proposed_classes
        entry["kept_vuln_classes"] = [s.vuln_class for s in kept if s.vuln_class]
        entry["scenario_names_kept_correct"] = [s.name for s in tagged_correct]

        if tagged_correct:
            entry["outcome"] = "caught"
            per_class[vclass]["caught"] += 1
        else:
            entry["outcome"] = "missed"
        details.append(entry)
        time.sleep(1)  # be polite to the API between real calls

    total = sum(c["total"] for c in per_class.values())
    caught = sum(c["caught"] for c in per_class.values())
    result = {
        "methodology": (
            "Same scope as phase0/'s original precision test: real {diff, intent} pairs "
            "through the real scenario-gen LLM call + real validator, no sandbox execution. "
            "A commit counts as caught only if a scenario tagged with the CORRECT vuln_class "
            "survived the validator's traceability check."
        ),
        "known_variance": (
            "generate() is called with force=True - deliberately bypassing the scenario-gen "
            "cache so every run tests actual current LLM behavior. That means identical diffs "
            "can still produce a different scenario set run-to-run (temperature=0 is not bit-exact "
            "on shared/batched cloud inference), and at only 5 examples per class, a single "
            "example moving in or out of 'caught' swings that class's recall by 20 points. Do not "
            "read small deltas between runs as regressions without checking which specific "
            "example changed outcome and why."
        ),
        "model": generation.model if "generation" in dir() else config.model,
        "provider": config.provider,
        "overall": {"total": total, "caught": caught, "recall": round(caught / total, 3) if total else None},
        "per_class": {
            k: {**v, "recall": round(v["caught"] / v["total"], 3) if v["total"] else None}
            for k, v in per_class.items()
        },
        "details": details,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    return result


if __name__ == "__main__":
    result = evaluate()
    RESULTS_PATH.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "details"}, indent=2))
    print(f"\nwrote {RESULTS_PATH}")
