"""
Hybrid mode (doc Section 8): autonomous + manual scenarios combined, deduped.

Deterministic merge. The developer's scenarios always survive dedupe -
when a generated scenario duplicates a manual one, the manual version wins,
because a human stating "test this" outranks a model guessing the same thing.
"""
import re
from dataclasses import dataclass

from verdict.generator import Scenario


@dataclass
class MergeResult:
    scenarios: list[Scenario]
    dropped_duplicates: list[str]  # names of generated scenarios shadowed by manual ones


def _normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def _description_terms(text: str) -> frozenset[str]:
    return frozenset(w for w in re.findall(r"[a-z]{4,}", text.lower()))


def _is_duplicate(generated: Scenario, manual: Scenario) -> bool:
    if _normalize(generated.name) == _normalize(manual.name):
        return True
    # descriptions that share most of their meaningful words are the same check
    g_terms = _description_terms(generated.description)
    m_terms = _description_terms(manual.description)
    if not g_terms or not m_terms:
        return False
    overlap = len(g_terms & m_terms) / min(len(g_terms), len(m_terms))
    return overlap >= 0.8


def merge(generated: list[Scenario], manual: list[Scenario]) -> MergeResult:
    """Manual scenarios first (developer authority), then non-duplicate generated ones."""
    kept: list[Scenario] = list(manual)
    dropped: list[str] = []
    for g in generated:
        if any(_is_duplicate(g, m) for m in manual):
            dropped.append(g.name)
        else:
            kept.append(g)
    return MergeResult(scenarios=kept, dropped_duplicates=dropped)
