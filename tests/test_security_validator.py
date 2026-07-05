"""
The security-shaped validator guards (Phase 6). Pure logic, no DB, no LLM.

These lock in the exact true-positive / true-negative behavior that the real
CVEfixes precision run surfaced and fixed - so a future edit to the regexes
can't silently reintroduce, e.g., the Perl ->Quote() blind spot that a real
run caught.
"""
from verdict.generator import Scenario
from verdict.validator import _unsupported_behavior_claim


def _reject(scenario, diff):
    return _unsupported_behavior_claim(scenario, diff)


class TestInjectionGuard:
    def test_rejects_prevention_claim_with_no_parameterization(self):
        s = Scenario("prevents_sql_injection",
                     "verify the query prevents SQL injection from user input", "injection")
        diff = '+ query = "SELECT * FROM users WHERE name = " + username\n+ cursor.execute(query)'
        assert _reject(s, diff) is not None

    def test_accepts_parameterized_query(self):
        s = Scenario("prevents_sql_injection",
                     "verify the query prevents SQL injection from user input", "injection")
        diff = '+ cursor.execute("SELECT * FROM users WHERE name = ?", (username,))'
        assert _reject(s, diff) is None

    def test_accepts_generic_quote_call(self):
        # The real CVEfixes miss: Perl DBI's ->Quote() escaping, which the
        # Python-only patterns missed until this was broadened.
        s = Scenario("prevents_sql_injection",
                     "verify the state values are quoted to prevent SQL injection", "injection")
        diff = '+    @StateType = map { $Self->{DBObject}->Quote($_) } @StateType;'
        assert _reject(s, diff) is None


class TestAuthBypassGuard:
    def test_rejects_auth_claim_with_no_check(self):
        s = Scenario("requires_admin_permission",
                     "verify the endpoint requires permission before deleting", "auth_bypass")
        diff = "+ def delete_user(user_id):\n+     db.delete(user_id)"
        assert _reject(s, diff) is not None

    def test_accepts_permission_decorator(self):
        s = Scenario("requires_admin_permission",
                     "verify the endpoint requires permission before deleting", "auth_bypass")
        diff = "+ @login_required\n+ def delete_user(user_id):\n+     db.delete(user_id)"
        assert _reject(s, diff) is None


class TestSecretLeakGuard:
    def test_rejects_redaction_claim_with_no_masking(self):
        s = Scenario("redacts_password",
                     "verify the password is redacted before being logged", "secret_leak")
        diff = "+ logger.info(f'user logged in with {password}')"
        assert _reject(s, diff) is not None

    def test_accepts_masking_construct(self):
        s = Scenario("redacts_password",
                     "verify the password is masked before being logged", "secret_leak")
        diff = "+ logger.info(f'user logged in with {redact(password)}')"
        assert _reject(s, diff) is None


class TestNonSecurityScenariosUnaffected:
    def test_plain_correctness_scenario_not_rejected(self):
        s = Scenario("returns_sorted_list", "verify the function returns items sorted ascending", None)
        diff = "+ return sorted(items)"
        assert _reject(s, diff) is None
