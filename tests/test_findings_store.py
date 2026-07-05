"""
Findings storage + full agent-chain integration, against the real Postgres
dev fixture. Marked @pytest.mark.postgres - auto-skips when no DB is
reachable, so a plain `pytest` stays green without Docker.

The LLM boundary is still mocked here: this proves the wiring (dual-write,
correlation persistence, Verification-requester flagging the OLDER finding,
Remediation-advisor attaching a suggestion) deterministically, without
depending on a live model's non-deterministic judgment - that judgment is
measured separately in hackathon/security_gen_eval/.
"""
import types

import pytest

pytestmark = pytest.mark.postgres


def _fake_response(text):
    return types.SimpleNamespace(text=text, prompt_tokens=0, output_tokens=0, duration_s=0.0)


def test_save_findings_returns_ids_and_reads_back(database_url, seeded_run):
    from verdict import store

    inserted = store.save_findings(database_url, seeded_run, "svc-a", [
        {"vuln_class": "injection", "name": "raw_concat", "description": "d",
         "severity": "HIGH", "evidence": "e", "source": "scenario"},
    ])
    assert len(inserted) == 1
    assert isinstance(inserted[0]["id"], int)
    assert inserted[0]["status"] == "open"

    back = store.get_finding(database_url, inserted[0]["id"])
    assert back["vuln_class"] == "injection"
    assert back["repo_name"] == "svc-a"


def test_correlation_only_links_never_mutates_verdict(database_url, seeded_run):
    from verdict import store

    a = store.save_findings(database_url, seeded_run, "svc-a", [
        {"vuln_class": "injection", "name": "first", "description": "d",
         "severity": "HIGH", "evidence": "e", "source": "scenario"}])[0]
    b = store.save_findings(database_url, seeded_run, "svc-b", [
        {"vuln_class": "injection", "name": "second", "description": "d",
         "severity": "HIGH", "evidence": "e", "source": "scenario"}])[0]

    store.set_correlation(database_url, b["id"], a["id"])
    linked = store.get_finding(database_url, b["id"])
    assert linked["correlated_with"] == a["id"]
    # correlation must not have touched severity or status
    assert linked["severity"] == "HIGH"
    assert linked["status"] == "open"


def test_full_agent_chain_via_run_agents(database_url, seeded_run, monkeypatch, tmp_path):
    """run_agents with a mocked LLM: the Correlator matches, so the
    Verification-requester flags the OLDER finding and the Remediation-advisor
    attaches a suggestion to the new one - all persisted, deterministically.

    The two findings must live in DIFFERENT runs: the Correlator deliberately
    excludes same-run findings (you don't correlate a finding against others
    from its own run), so a second run is created here for the older one."""
    from datetime import datetime, timezone

    from verdict import findings as findings_mod
    from verdict import store
    from verdict.agents import correlator, remediation

    older_run = seeded_run + "_older"
    store.save_run_record(database_url, {
        "run_id": older_run, "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "completed", "risk": {"level": "HIGH"}})
    try:
        older = store.save_findings(database_url, older_run, "svc-a", [
            {"vuln_class": "injection", "name": "older_injection", "description": "old",
             "severity": "HIGH", "evidence": "e", "source": "scenario"}])[0]
        newer = store.save_findings(database_url, seeded_run, "svc-b", [
            {"vuln_class": "injection", "name": "newer_injection", "description": "new",
             "severity": "HIGH", "evidence": "e", "source": "scenario"}])[0]

        # Correlator and Remediation both call the SAME verdict.llm.call, so a
        # single mock routes on the prompt: the correlation prompt asks to
        # correlate, the remediation prompt asks for a fix. (Patching them
        # separately would just overwrite each other - same module object.)
        def _routed_call(prompt, *a, **k):
            if "correlating security findings" in prompt:
                return _fake_response(f'{{"match_id": {older["id"]}, "reason": "same"}}')
            return _fake_response("Use a parameterized query.")

        monkeypatch.setattr(correlator.llm, "call", _routed_call)

        findings_mod.run_agents([newer], tmp_path, config=object(), database_url=database_url)

        older_after = store.get_finding(database_url, older["id"])
        newer_after = store.get_finding(database_url, newer["id"])

        assert newer_after["correlated_with"] == older["id"]        # Correlator wrote the link
        assert older_after["reverification_reason"] is not None      # Verification-requester flagged the OLDER one
        assert newer_after["suggested_fix"] == "Use a parameterized query."  # Remediation-advisor attached a fix
        # the one rule that never bends: no agent closed anything out
        assert older_after["status"] == "open"
        assert newer_after["status"] in ("open", "alerted")
    finally:
        with store.connect(database_url) as conn:
            conn.execute("DELETE FROM runs WHERE run_id = %s", (older_run,))
