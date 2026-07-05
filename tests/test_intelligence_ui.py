"""
The /intelligence page must HTML-escape free-form text (suggested_fix,
description) before embedding it - that text can come from an LLM or,
transitively, from a diff, so it is not trusted markup. This locks in the
escaping so a future template edit can't reopen an injection hole.

Uses FastAPI's TestClient with the store layer stubbed - no Postgres needed.
"""
import pytest

pytest.importorskip("fastapi")


def test_intelligence_escapes_untrusted_text(monkeypatch):
    from fastapi.testclient import TestClient

    from verdict.server import api

    malicious = "<script>alert('xss')</script> & <b>bold</b>"
    fake_findings = [{
        "id": 1, "run_id": "r", "repo_name": "svc-a", "vuln_class": "injection",
        "name": "f", "description": malicious, "severity": "HIGH", "evidence": "e",
        "source": "scenario", "status": "open", "created_at": "2026-01-01",
        "correlated_with": None, "suggested_fix": malicious, "reverification_reason": None,
    }]
    monkeypatch.setattr(api.store, "list_findings", lambda *a, **k: fake_findings)
    monkeypatch.setattr(api, "database_url", lambda: "stub")
    # the route is auth-gated; clear any key requirement for the test
    monkeypatch.delenv("VERDICT_SERVER_API_KEY", raising=False)

    client = TestClient(api.app)
    resp = client.get("/intelligence")
    assert resp.status_code == 200
    # the raw script tag must NOT appear; its escaped form must
    assert "<script>alert('xss')</script>" not in resp.text
    assert "&lt;script&gt;" in resp.text


def test_intelligence_renders_empty_state(monkeypatch):
    from fastapi.testclient import TestClient

    from verdict.server import api

    monkeypatch.setattr(api.store, "list_findings", lambda *a, **k: [])
    monkeypatch.setattr(api, "database_url", lambda: "stub")
    monkeypatch.delenv("VERDICT_SERVER_API_KEY", raising=False)

    client = TestClient(api.app)
    resp = client.get("/intelligence")
    assert resp.status_code == 200
    assert "no findings yet" in resp.text
