"""SupabaseStore and the flows migration against Supabase's Postgres and PostgREST (Docker)."""

import urllib.error
import urllib.request
from importlib import resources

import pytest
from stack import ANON, SERVICE, docker, jwt, postgres_rest, proxy
from stack import psql as stack_psql

from supie.flow import Flow, StepFailed, SupabaseStore
from supie.testing import run

pytestmark = pytest.mark.postgres


@pytest.fixture(scope="module")
def rest(images):
    """Postgres with the flows migration applied (twice: it's idempotent), and PostgREST."""
    sql = resources.files("supie").joinpath("sql").joinpath("flows.sql").read_text()

    def setup(db):
        """Apply the migration twice."""
        for _ in range(2):
            stack_psql(db, sql)

    with postgres_rest(images, setup) as stack:
        yield stack


def flow_with(store, fail=False):
    """A two-step flow with a branch, kept in Supabase."""
    flow = Flow("pg-scoring", store)

    @flow.step()
    def score(ctx, input):
        """Score the applicant."""
        return {"pd": 0.3 if input["risky"] else 0.01}

    @flow.step(depends_on=[score])
    def decide(ctx, input, score):
        """Decide, or go to review."""
        if fail:
            raise RuntimeError("decision service down")
        if score["pd"] > 0.1:
            return ctx.goto(review)
        return {"decision": "approve"}

    @flow.branch(depends_on=[score])
    def review(ctx, input, score):
        """Manual review."""
        return {"decision": "refer"}

    return flow


def psql(db, query):
    """Run a query in the test database and return the rows as text."""
    return docker(
        "exec", db, "psql", "-U", "postgres", "-h", "localhost", "-At", "-c", query
    ).stdout.strip()


def test_runs_and_steps_are_saved(rest):
    """A run, its steps and its goto land in the tables."""
    base, db = rest
    env = {"SUPABASE_SERVICE_ROLE_KEY": SERVICE}
    result = run(
        flow_with(SupabaseStore()).run({"risky": True}), env=env, fetch=proxy(base)
    )
    assert result.output == {"decision": "refer"}
    assert (
        psql(
            db,
            f"select status, output->>'decision' from supie_flow_runs where id = '{result.run_id}'",
        )
        == "succeeded|refer"
    )
    steps = psql(
        db,
        f"select step, status, coalesce(goto, '-') from supie_flow_steps where run_id = '{result.run_id}' order by step",
    )
    assert steps.splitlines() == [
        "decide|succeeded|review",
        "review|succeeded|-",
        "score|succeeded|-",
    ]


def test_failure_resume_and_stale(rest):
    """A failed step is saved; the run resumes from it and shows up as stale until then."""
    base, db = rest
    env = {"SUPABASE_SERVICE_ROLE_KEY": SERVICE}
    fetch = proxy(base)
    with pytest.raises(StepFailed):
        run(
            flow_with(SupabaseStore(), fail=True).run({"risky": False}),
            env=env,
            fetch=fetch,
        )
    run_id = psql(db, "select id from supie_flow_runs where status = 'failed'")
    assert "decision service down" in psql(
        db,
        f"select error from supie_flow_steps where run_id = '{run_id}' and step = 'decide'",
    )

    # backdate the run with triggers off, or the touch trigger would reset updated_at
    psql(
        db,
        "set session_replication_role = replica; "
        f"update supie_flow_runs set status = 'running', updated_at = now() - interval '1 hour' where id = '{run_id}'",
    )
    stale = run(SupabaseStore().stale("pg-scoring", 600), env=env, fetch=fetch)
    assert stale == [run_id]

    resumed = run(
        flow_with(SupabaseStore()).run(None, run_id=run_id), env=env, fetch=fetch
    )
    assert resumed.output == {"decision": "approve"}
    assert (
        psql(db, f"select status from supie_flow_runs where id = '{run_id}'")
        == "succeeded"
    )
    assert run(SupabaseStore().stale("pg-scoring", 0), env=env, fetch=fetch) == []


def test_anon_and_authenticated_are_locked_out(rest):
    """Only the service role can reach the tables and the stale function."""
    base, _ = rest
    user = jwt({"role": "authenticated", "sub": "u1"})
    for token in (ANON, user):
        for path, method, body in [
            ("/supie_flow_runs", "GET", None),
            ("/supie_flow_steps", "GET", None),
            ("/rpc/supie_flow_stale", "POST", b'{"flow_name": "x", "idle_seconds": 0}'),
        ]:
            request = urllib.request.Request(
                base + path,
                data=body,
                method=method,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
            with pytest.raises(urllib.error.HTTPError) as denied:
                urllib.request.urlopen(request, timeout=10)
            assert denied.value.code in (401, 403, 404), path
