"""SupabaseStore and the flows migration against Supabase's Postgres and PostgREST (Docker)."""

import base64
import hashlib
import hmac
import json
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from importlib import resources

import pytest

from supie.flow import Flow, StepFailed, SupabaseStore
from supie.testing import FakeResponse, run

pytestmark = pytest.mark.postgres

SECRET = "super-secret-jwt-token-with-at-least-32-characters"


def jwt(claims):
    """An HS256 JWT signed with the test secret."""

    def part(data):
        """Base64url-encode JSON without padding."""
        raw = json.dumps(data, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    head = part({"alg": "HS256", "typ": "JWT"}) + "." + part(claims)
    sig = hmac.new(SECRET.encode(), head.encode(), hashlib.sha256).digest()
    return head + "." + base64.urlsafe_b64encode(sig).decode().rstrip("=")


SERVICE = jwt({"role": "service_role"})
ANON = jwt({"role": "anon"})


def _free_port():
    """A free local TCP port."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _docker(*args, **kwargs):
    """Run a docker command, failing loudly."""
    return subprocess.run(
        ["docker", *args], check=True, capture_output=True, text=True, **kwargs
    )


@pytest.fixture(scope="module")
def rest(images):
    """Postgres with the migration applied, and PostgREST in front of it."""
    if not shutil.which("docker") or subprocess.call(
        ["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    ):
        pytest.skip("needs Docker")
    tag = uuid.uuid4().hex[:8]
    network, db, api = f"supie-net-{tag}", f"supie-pg-{tag}", f"supie-rest-{tag}"
    port = _free_port()
    _docker("network", "create", network)
    try:
        _docker(
            "run",
            "-d",
            "--name",
            db,
            "--network",
            network,
            "-e",
            "POSTGRES_PASSWORD=postgres",
            images["postgres"],
        )
        sql = resources.files("supie").joinpath("sql", "flows.sql").read_text()
        for _ in range(120):
            ready = subprocess.run(
                [
                    "docker",
                    "exec",
                    db,
                    "psql",
                    "-U",
                    "postgres",
                    "-h",
                    "localhost",
                    "-c",
                    "select 1 from pg_roles where rolname = 'service_role'",
                ],
                capture_output=True,
                text=True,
            )
            if ready.returncode == 0 and "1 row" in ready.stdout:
                break
            time.sleep(0.5)
        for _ in range(2):  # the migration is safe to run twice
            _docker(
                "exec",
                "-i",
                db,
                "psql",
                "-U",
                "postgres",
                "-h",
                "localhost",
                "-v",
                "ON_ERROR_STOP=1",
                "-q",
                input=sql,
            )
        _docker(
            "run",
            "-d",
            "--name",
            api,
            "--network",
            network,
            "-p",
            f"127.0.0.1:{port}:3000",
            "-e",
            f"PGRST_DB_URI=postgres://postgres:postgres@{db}:5432/postgres",
            "-e",
            "PGRST_DB_SCHEMAS=public",
            "-e",
            "PGRST_DB_ANON_ROLE=anon",
            "-e",
            f"PGRST_JWT_SECRET={SECRET}",
            images["postgrest"],
        )
        base = f"http://127.0.0.1:{port}"
        for _ in range(150):  # 503 until PostgREST has loaded the schema cache
            try:
                urllib.request.urlopen(base + "/", timeout=2)
                break
            except OSError:
                time.sleep(0.2)
        else:
            pytest.fail("PostgREST didn't become ready")
        yield base, db
    finally:
        subprocess.run(["docker", "rm", "-f", api, db], capture_output=True)
        subprocess.run(["docker", "network", "rm", network], capture_output=True)


def proxy(base):
    """A fetch that sends the Supabase client's REST calls to the local PostgREST."""

    def fetch(call):
        """Forward one call and return PostgREST's answer."""
        url = call.url.replace("https://test.supabase.co/rest/v1", base)
        data = call.body.encode() if isinstance(call.body, str) else call.body
        request = urllib.request.Request(
            url, data=data, method=call.method, headers=call.headers
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return FakeResponse(
                    response.read().decode(), response.status, dict(response.headers)
                )
        except urllib.error.HTTPError as error:
            return FakeResponse(error.read().decode(), error.code, dict(error.headers))

    return fetch


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
    return _docker(
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
