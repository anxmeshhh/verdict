"""
Phase 0 - Step 3: manual labeling.

Run this yourself in a terminal:  python label.py

For each generated scenario, you judge: given the diff and the stated
intent, is this a legitimate, concrete test scenario worth running?
Answers save after every response, so you can Ctrl+C or 'q' any time
and resume later - nothing is lost.
"""
import json
from pathlib import Path

SCENARIOS_FILE = Path(__file__).parent / "scenarios.json"
LABELS_FILE = Path(__file__).parent / "labels.json"


def load_labels():
    if LABELS_FILE.exists():
        return json.loads(LABELS_FILE.read_text(encoding="utf-8"))
    return {}


def save_labels(labels):
    LABELS_FILE.write_text(json.dumps(labels, indent=2), encoding="utf-8")


def main():
    entries = json.loads(SCENARIOS_FILE.read_text(encoding="utf-8"))
    labels = load_labels()

    all_items = []
    for entry in entries:
        for scenario in entry["scenarios"]:
            key = f"{entry['id']}::{scenario['name']}"
            all_items.append((key, entry, scenario))

    total = len(all_items)
    remaining = [item for item in all_items if item[0] not in labels]

    print(f"\n{total} total scenarios, {len(labels)} already labeled, {len(remaining)} remaining.\n")

    for i, (key, entry, scenario) in enumerate(remaining, 1):
        print("=" * 70)
        print(f"[{len(labels) + 1}/{total}]  commit {entry['id']}")
        print(f"Stated intent: {entry['intent']}")
        print("-" * 70)
        diff_lines = entry["diff"].splitlines()
        preview = "\n".join(diff_lines[:35])
        print(preview)
        if len(diff_lines) > 35:
            print(f"... ({len(diff_lines) - 35} more lines, press 'd' to see full diff)")
        print("-" * 70)
        print(f"Scenario: {scenario['name']}")
        print(f"  {scenario['description']}")
        print("-" * 70)

        while True:
            ans = input("Valid, concrete test scenario for this intent? [y/n/d=full diff/s=skip/q=quit]: ").strip().lower()
            if ans == "d":
                print(entry["diff"])
                continue
            if ans in ("y", "n"):
                labels[key] = (ans == "y")
                save_labels(labels)
                break
            if ans == "s":
                break
            if ans == "q":
                print(f"\nSaved. Labeled {len(labels)}/{total} so far. Resume anytime with: python label.py")
                return
            print("Please answer y, n, d, s, or q.")

    print(f"\nAll done. Labeled {len(labels)}/{total}. Run: python report.py")


if __name__ == "__main__":
    main()
