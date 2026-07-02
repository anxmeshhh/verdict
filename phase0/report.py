"""Phase 0 - Step 4: compute precision against the doc's Section 12 gate (>70%)."""
import json
from pathlib import Path

LABELS_FILE = Path(__file__).parent / "labels.json"
GATE = 0.70


def main():
    if not LABELS_FILE.exists():
        print("No labels.json yet - run label.py first.")
        return

    labels = json.loads(LABELS_FILE.read_text(encoding="utf-8"))
    if not labels:
        print("labels.json is empty - run label.py first.")
        return

    total = len(labels)
    valid = sum(1 for v in labels.values() if v)
    precision = valid / total

    print(f"Labeled scenarios: {total}")
    print(f"Valid:             {valid}")
    print(f"Invalid:           {total - valid}")
    print(f"Precision:         {precision:.1%}")
    print(f"Gate (Section 12): {GATE:.0%}")
    print()
    if precision > GATE:
        print("PASS - core assumption holds. Proceed to Phase 1.")
    else:
        print("FAIL - below gate. Per Section 12: stop, no architecture fixes a broken core idea.")


if __name__ == "__main__":
    main()
