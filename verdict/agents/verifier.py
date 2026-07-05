"""
Verification-requester agent (Verdict Intelligence).

Autonomous, event-triggered: runs right after the Correlator finds a match,
no human prompt. Its one job - flag the OLDER finding (the one a new
correlation just matched against) as worth a real re-check, since the same
vulnerability pattern resurfacing elsewhere is a signal the original fix may
not have been as complete as it looked.

Deliberately scoped to flagging, not re-running: this agent does not have a
checked-out copy of whatever repo the older finding came from (Verdict Core
only ever has the current run's repo on disk), so actually re-invoking a
sandboxed scan there would mean fabricating access this tool doesn't have.
What it *can* do honestly - and does - is make sure the request to look
again is recorded and visible, not silently lost. The only thing that can
ever confirm a finding is still valid (or is fixed) is a real Verdict Core
run against that repo, same rule as everywhere else in this project.
"""
from verdict import store


def request_reverification(matched_finding_id: int, new_finding: dict, database_url: str) -> str:
    """Writes the reverification_reason onto the OLDER, matched finding.
    Always succeeds or raises - there's no LLM call and no ambiguity here,
    it's a direct consequence of the Correlator's own (already-trusted)
    match, not a new judgment call."""
    reason = (
        f"Same {new_finding['vuln_class']} pattern reappeared in "
        f"{new_finding.get('repo_name') or 'another repo'} (finding #{new_finding['id']}) - "
        "worth a real re-check here in case this fix wasn't complete, or the pattern was "
        "reintroduced by a later change."
    )
    store.set_reverification_reason(database_url, matched_finding_id, reason)
    return reason
