"""
Agent-layer unit tests. The LLM boundary (llm.call) and the store are both
mocked, so these run with no API key and no Postgres - they test the agents'
own logic, which is where the trust-critical rules live (don't trust a
hallucinated match id; only escalate on the right signals).
"""
import types

import pytest

from verdict.agents import correlator, triage


# --- Triage: pure deterministic ranking, no LLM, no DB --------------------

class TestTriageShouldAlert:
    def test_high_severity_alerts(self):
        assert triage._should_alert({"severity": "HIGH", "correlated_with": None}) is not None

    def test_critical_severity_alerts(self):
        assert triage._should_alert({"severity": "CRITICAL", "correlated_with": None}) is not None

    def test_low_severity_does_not_alert(self):
        assert triage._should_alert({"severity": "LOW", "correlated_with": None}) is None

    def test_medium_severity_does_not_alert_on_its_own(self):
        assert triage._should_alert({"severity": "MEDIUM", "correlated_with": None}) is None

    def test_recurrence_alerts_even_at_low_severity(self):
        # A repeat of a past finding is escalated regardless of nominal severity.
        reason = triage._should_alert({"severity": "LOW", "correlated_with": 42})
        assert reason is not None
        assert "recurrence" in reason.lower()

    def test_high_and_recurrence_mentions_both(self):
        reason = triage._should_alert({"severity": "HIGH", "correlated_with": 42})
        assert reason is not None
        assert "repeat" in reason.lower() or "recurrence" in reason.lower()


# --- Correlator: must never trust a match id outside the candidate list ---

def _fake_llm_response(text):
    return types.SimpleNamespace(text=text, prompt_tokens=0, output_tokens=0, duration_s=0.0)


class _FakeStore:
    """Stands in for verdict.store: fixed candidate list, records any
    set_correlation call so the test can assert whether the agent wrote."""

    def __init__(self, candidates):
        self._candidates = candidates
        self.correlated = []

    def list_findings(self, url, vuln_class=None, exclude_run_id=None, limit=None):
        return self._candidates

    def set_correlation(self, url, finding_id, matched_id):
        self.correlated.append((finding_id, matched_id))


@pytest.fixture
def new_finding():
    return {"id": 100, "run_id": "run_new", "vuln_class": "injection",
            "name": "new_injection", "description": "unescaped user input in a query",
            "repo_name": "svc-a"}


def _patch(monkeypatch, store_stub, llm_text):
    monkeypatch.setattr(correlator, "store", store_stub)
    monkeypatch.setattr(correlator.llm, "call", lambda *a, **k: _fake_llm_response(llm_text))


def test_correlator_accepts_valid_match(monkeypatch, new_finding):
    candidates = [{"id": 7, "name": "old_injection", "repo_name": "svc-b",
                   "created_at": "2026-01-01", "description": "same pattern"}]
    store_stub = _FakeStore(candidates)
    _patch(monkeypatch, store_stub, '{"match_id": 7, "reason": "same root cause"}')
    result = correlator.correlate(new_finding, config=object(), database_url="x")
    assert result is not None
    assert result.matched_finding_id == 7
    assert store_stub.correlated == [(100, 7)]


def test_correlator_rejects_hallucinated_id(monkeypatch, new_finding):
    # Model returns an id that is NOT in the candidate list - must be
    # discarded, never written. This is the trust-critical guard.
    candidates = [{"id": 7, "name": "old_injection", "repo_name": "svc-b",
                   "created_at": "2026-01-01", "description": "same pattern"}]
    store_stub = _FakeStore(candidates)
    _patch(monkeypatch, store_stub, '{"match_id": 999, "reason": "made this up"}')
    result = correlator.correlate(new_finding, config=object(), database_url="x")
    assert result is None
    assert store_stub.correlated == []


def test_correlator_handles_no_match(monkeypatch, new_finding):
    candidates = [{"id": 7, "name": "old", "repo_name": "svc-b",
                   "created_at": "2026-01-01", "description": "different"}]
    store_stub = _FakeStore(candidates)
    _patch(monkeypatch, store_stub, '{"match_id": null, "reason": ""}')
    result = correlator.correlate(new_finding, config=object(), database_url="x")
    assert result is None
    assert store_stub.correlated == []


def test_correlator_no_candidates_short_circuits(monkeypatch, new_finding):
    store_stub = _FakeStore([])
    called = {"llm": False}

    def _boom(*a, **k):
        called["llm"] = True
        raise AssertionError("llm.call must not run when there are no candidates")

    monkeypatch.setattr(correlator, "store", store_stub)
    monkeypatch.setattr(correlator.llm, "call", _boom)
    result = correlator.correlate(new_finding, config=object(), database_url="x")
    assert result is None
    assert called["llm"] is False


def test_correlator_unparseable_response_is_safe(monkeypatch, new_finding):
    candidates = [{"id": 7, "name": "old", "repo_name": "svc-b",
                   "created_at": "2026-01-01", "description": "same"}]
    store_stub = _FakeStore(candidates)
    _patch(monkeypatch, store_stub, "not json at all")
    result = correlator.correlate(new_finding, config=object(), database_url="x")
    assert result is None
    assert store_stub.correlated == []
