"""
Module 6 - Risk Scorer.

Input:  sandbox results
Output: RiskReport - LOW / MEDIUM / HIGH / UNVERIFIED with explicit reasons

Pure deterministic logic. Every level is traceable to counts a human can
recheck by hand. "uncertain", "error" and "timeout" results are non-evidence:
they never count for OR against the change (a broken check proves nothing).
If nothing conclusive ran, the verdict is UNVERIFIED - never a confident LOW
on zero evidence (Section 13: silence beats a wrong verdict).

Doc-example calibration (Section 8): 1/3 passed -> coverage 33% -> HIGH.

Deliberate, accepted limitation (decided, not emergent): a scenario that
comes back "uncertain" every time - because the generated test itself is
broken, not because the code is - is risk-neutral forever under this scheme.
It costs nothing, the same way a FAILED scenario costs real coverage. In
principle that means code whose behavior happens to produce hard-to-test-
looking scenarios could score better than it should. We accept this for now
because the alternative (treating a bad test as evidence against the code)
is worse - a broken check proving nothing must never be scored as if it
proved something. If this needs revisiting (e.g. an inconclusive-rate cap
that forces UNVERIFIED past some threshold), it should be a deliberate
scoring-policy change, not something bolted on quietly.
"""
from dataclasses import dataclass, field

from verdict.sandbox import SandboxResult

# Below this pass-rate over conclusive checks, the change is HIGH risk
HIGH_RISK_THRESHOLD = 2 / 3


@dataclass
class RiskReport:
    level: str  # "LOW" | "MEDIUM" | "HIGH" | "UNVERIFIED"
    passed: int
    failed: int
    inconclusive: int  # uncertain + error + timeout
    coverage: float | None  # passed / (passed+failed), None when no conclusive runs
    reasons: list[str] = field(default_factory=list)


def score(results: list[SandboxResult]) -> RiskReport:
    passed = [r for r in results if r.status == "passed"]
    failed = [r for r in results if r.status == "failed"]
    inconclusive = [r for r in results if r.status in ("uncertain", "error", "timeout")]

    reasons: list[str] = []
    for r in failed:
        reasons.append(f"FAILED  {r.scenario_name} (exit {r.exit_code})")
    for r in inconclusive:
        reasons.append(f"NO-EVIDENCE  {r.scenario_name} ({r.status})")

    conclusive = len(passed) + len(failed)
    if conclusive == 0:
        reasons.insert(0, "no scenario produced conclusive evidence - human review required")
        return RiskReport(
            level="UNVERIFIED",
            passed=0,
            failed=0,
            inconclusive=len(inconclusive),
            coverage=None,
            reasons=reasons,
        )

    coverage = len(passed) / conclusive
    if not failed:
        level = "LOW"
        reasons.insert(0, f"all {len(passed)} conclusive scenario(s) passed")
    elif coverage >= HIGH_RISK_THRESHOLD:
        level = "MEDIUM"
        reasons.insert(0, f"{len(failed)} scenario(s) failed, coverage {coverage:.0%}")
    else:
        level = "HIGH"
        reasons.insert(0, f"only {len(passed)}/{conclusive} conclusive scenario(s) passed ({coverage:.0%})")

    return RiskReport(
        level=level,
        passed=len(passed),
        failed=len(failed),
        inconclusive=len(inconclusive),
        coverage=coverage,
        reasons=reasons,
    )
