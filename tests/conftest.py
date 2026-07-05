"""
Shared pytest fixtures.

The agent-layer tests are split by what they actually need:
- Most mock the LLM boundary (llm.call) and never touch a database - they
  run anywhere, no infrastructure.
- A few genuinely exercise the Postgres dual-write / correlation storage;
  those are marked @pytest.mark.postgres and auto-skip unless the dev
  fixture on :5433 is reachable, so a plain `pytest` run is always green
  without Docker.
"""
import os
import uuid

import pytest

DEV_DATABASE_URL = os.environ.get(
    "VERDICT_TEST_DATABASE_URL", "postgresql://verdict:verdict@localhost:5433/verdict"
)


def _postgres_reachable(url: str) -> bool:
    try:
        from verdict import store

        ok, _ = store.check(url)
        return ok
    except Exception:
        return False


@pytest.fixture(scope="session")
def database_url():
    if not _postgres_reachable(DEV_DATABASE_URL):
        pytest.skip("no reachable Postgres dev fixture (:5433) - skipping DB-backed tests")
    from verdict import store

    store.init_schema(DEV_DATABASE_URL)
    return DEV_DATABASE_URL


@pytest.fixture
def seeded_run(database_url):
    """A throwaway run row (findings FK-reference runs) plus guaranteed
    cleanup of everything created under it, so DB tests never leak rows
    into the shared dev fixture."""
    from datetime import datetime, timezone

    from verdict import store

    run_id = f"pytest_{uuid.uuid4().hex[:12]}"
    store.save_run_record(
        database_url,
        {
            "run_id": run_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "completed",
            "risk": {"level": "HIGH"},
        },
    )
    yield run_id
    with store.connect(database_url) as conn:
        conn.execute("DELETE FROM runs WHERE run_id = %s", (run_id,))
