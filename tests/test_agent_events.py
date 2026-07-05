"""
Agent activity stream (verdict/agents/events.py). No DB, no LLM - pure
file + text handling.
"""
from verdict.agents import events


def test_emit_then_read_roundtrip(tmp_path):
    events.emit(tmp_path, "correlator", "matched", "same as finding #7", finding_id=12)
    events.emit(tmp_path, "triage", "alerted", "HIGH severity", finding_id=12)
    got = events.read_recent(tmp_path)
    assert len(got) == 2
    assert got[0]["agent"] == "correlator"
    assert got[0]["action"] == "matched"
    assert got[0]["finding_id"] == 12
    assert got[1]["agent"] == "triage"


def test_read_recent_respects_limit(tmp_path):
    for i in range(10):
        events.emit(tmp_path, "correlator", "reviewing", f"finding {i}", finding_id=i)
    got = events.read_recent(tmp_path, limit=3)
    assert len(got) == 3
    # newest-tail: the last three emitted
    assert [e["finding_id"] for e in got] == [7, 8, 9]


def test_read_recent_empty_when_no_file(tmp_path):
    assert events.read_recent(tmp_path) == []


def test_detail_is_ascii_normalized(tmp_path):
    # A non-breaking hyphen + em dash + smart quote - exactly the kind of
    # thing an LLM emits that a cp1252 Windows console crashes on.
    events.emit(tmp_path, "remediation", "suggested", "use‑a — “prepared” statement")
    got = events.read_recent(tmp_path)
    detail = got[0]["detail"]
    assert detail == 'use-a - "prepared" statement'
    # guarantee it's encodable by a legacy console
    detail.encode("cp1252")


def test_emit_never_raises_on_bad_path(tmp_path):
    # A path whose parent can't be created must be swallowed, not raised -
    # logging activity must never break the activity.
    bad = tmp_path / "a_file"
    bad.write_text("i am a file, not a dir")
    # repo/.verdict/... under a file path will fail to mkdir; emit swallows it
    events.emit(bad, "correlator", "reviewing", "should not raise")
