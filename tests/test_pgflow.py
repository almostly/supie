"""Durable flows on pgflow, against Supabase's Postgres with pgflow and PostgREST (Docker)."""

import time
import urllib.error
import urllib.request
from importlib import resources

import pytest
from stack import (
    ANON,
    REALTIME_STUB,
    SERVICE,
    jwt,
    pgflow_migrations,
    postgres_rest,
    proxy,
    psql,
)

from supie import App, sleep
from supie.flow import Flow
from supie.testing import TestClient, run

pytestmark = [pytest.mark.postgres, pytest.mark.network]


@pytest.fixture(scope="module")
def rest(images):
    """Postgres with pgflow and supie's pgflow functions (applied twice), and PostgREST."""
    bridge = resources.files("supie").joinpath("sql").joinpath("pgflow.sql").read_text()

    def setup(db):
        """Install pgflow, then the bridge."""
        psql(db, REALTIME_STUB, user="supabase_admin")
        for migration in pgflow_migrations():
            psql(db, migration.read_text(), transaction=True)
        for _ in range(2):
            psql(db, bridge, transaction=True)

    with postgres_rest(images, setup) as stack:
        yield stack


ENV = {"SUPABASE_SERVICE_ROLE_KEY": SERVICE}


def durable(base, coro):
    """Await something that talks to pgflow through the local PostgREST."""
    return run(coro, env=ENV, fetch=proxy(base))


def routing(name, calls=None, flaky=False):
    """Two parallel checks, a decision, and a route chosen with when=."""
    calls = [] if calls is None else calls
    flow = Flow(name)

    @flow.step()
    async def bureau(ctx, application):
        """A slow check."""
        await sleep(0.5)
        calls.append("bureau")
        return {"score": 700}

    @flow.step()
    async def fraud(ctx, application):
        """Another slow check, in parallel."""
        await sleep(0.5)
        calls.append("fraud")
        return {"flag": application.get("suspicious", False)}

    @flow.step(depends_on=[bureau, fraud], retries=2, backoff=1)
    def decide(ctx, application, bureau, fraud):
        """Pick a route; fails its first attempt when flaky."""
        calls.append(("decide", ctx.attempt))
        if flaky and ctx.attempt == 1:
            raise RuntimeError("decision service down")
        return {"route": "review" if fraud["flag"] else "approve"}

    @flow.step(depends_on=[decide], when={"decide": {"route": "approve"}})
    def approve(ctx, application, decide):
        """The approve route."""
        return {"decision": "approve", "id": application["id"]}

    @flow.step(depends_on=[decide], when={"decide": {"route": "review"}})
    def review(ctx, application, decide):
        """The review route."""
        return {"decision": "refer", "id": application["id"]}

    return flow


def test_durable_run_with_parallel_steps_and_a_route(rest):
    """pgflow schedules the steps; a worker runs them; the unmet route is skipped."""
    base, db = rest
    calls = []
    flow = routing("durable_routing", calls)

    async def go():
        """Start, work, read."""
        run_id = await flow.start({"id": 7, "suspicious": True})
        started = time.perf_counter()
        ran = await flow.work(seconds=20, idle_polls=1)
        return run_id, ran, time.perf_counter() - started, await flow.status(run_id)

    run_id, ran, elapsed, state = durable(base, go())
    assert ran == 4
    assert state["status"] == "completed"
    assert state["output"] == {"review": {"decision": "refer", "id": 7}}
    assert state["steps"]["approve"]["status"] == "skipped"
    assert {"bureau", "fraud"} <= set(calls[:2])
    assert elapsed < 4, "bureau and fraud should overlap"
    assert (
        psql(db, f"select status from pgflow.runs where run_id = '{run_id}'")
        == "completed"
    )


def test_durable_retry(rest):
    """A failed attempt goes back to pgflow, which retries after the backoff."""
    base, _ = rest
    calls = []
    flow = routing("durable_retry", calls, flaky=True)

    async def go():
        """Start and work until done."""
        run_id = await flow.start({"id": 8})
        await flow.work(seconds=30, idle_polls=3)
        return await flow.status(run_id)

    state = durable(base, go())
    assert state is not None and state["status"] == "completed", state
    assert ("decide", 1) in calls and ("decide", 2) in calls
    assert state["output"] == {"approve": {"decision": "approve", "id": 8}}


def test_durable_failure(rest):
    """When the attempts run out, the step and the run fail, with the error kept."""
    base, _ = rest
    flow = Flow("durable_failure")

    @flow.step(retries=0)
    def explode(ctx, input):
        """Always fail."""
        raise ValueError("bad input")

    async def go():
        """Start and work."""
        run_id = await flow.start({})
        await flow.work(seconds=10, idle_polls=1)
        return await flow.status(run_id)

    state = durable(base, go())
    assert state["status"] == "failed"
    assert "ValueError: bad input" in state["steps"]["explode"]["error"]


def test_mounted_durable_endpoints(rest):
    """POST starts and works a run, GET reads it, and users only see their own runs."""
    base, _ = rest
    app = App()
    routing("durable_mounted").mount(app, "/flows/scoring", durable=True)
    client = TestClient(app, env=ENV, fetch=proxy(base))
    alice = {
        "Authorization": f"Bearer {jwt({'role': 'authenticated', 'sub': 'alice'})}"
    }

    started = client.post("/flows/scoring", json={"id": 9}, headers=alice)
    assert started.status == 202
    run_id = started.json()["run_id"]
    path = f"/flows/scoring/{run_id}"
    state = client.get(path, headers=alice).json()
    assert state["status"] == "completed"
    assert state["output"] == {"approve": {"decision": "approve", "id": 9}}
    bob = {"Authorization": f"Bearer {jwt({'role': 'authenticated', 'sub': 'bob'})}"}
    assert client.get(path, headers=bob).status == 404
    service = {"Authorization": f"Bearer {SERVICE}"}
    assert client.get(path, headers=service).status == 200
    assert client.post("/flows/scoring/work").status == 403
    assert client.post("/flows/scoring/work?seconds=2", headers=service).json() == {
        "ran": 0
    }


def test_bridge_is_service_role_only(rest):
    """anon and signed-in users can't call the supie_pgflow functions."""
    base, _ = rest
    user = jwt({"role": "authenticated", "sub": "u1"})
    for token in (ANON, user):
        request = urllib.request.Request(
            base + "/rpc/supie_pgflow_start",
            data=b'{"flow_slug": "x", "input": {}}',
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        with pytest.raises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(request, timeout=10)
        assert denied.value.code in (401, 403, 404)


@pytest.fixture(scope="module")
def edge_pgflow(rest, tmp_path_factory, images):
    """The durable flow built for both runtimes, served next to pgflow's PostgREST."""
    import json
    import shutil

    from stack import EDGE, edge_runtime

    from supie import build

    root = tmp_path_factory.mktemp("pgflow-edge")
    (root / "supabase").mkdir()
    (root / "supabase" / "config.toml").write_text('project_id = "p"\n')
    for name, flow_name in [
        ("durable", "edge_mpy"),
        ("durable-pyodide", "edge_pyodide"),
    ]:
        shutil.copytree(EDGE / "durable", root / "py" / name)
        main = root / "py" / name / "main.py"
        main.write_text(main.read_text().replace("edge_scoring", flow_name))
    (root / "supie.toml").write_text(
        '[functions.durable-pyodide]\nruntime = "pyodide"\n'
    )
    project = build.load_project(root)
    for name in ("durable", "durable-pyodide"):
        build.build(project, project.functions[name])
    functions = root / "supabase" / "functions"
    with edge_runtime(rest, images, functions, "/durable/shape") as (base, container):
        yield base, json, container


@pytest.mark.edge
@pytest.mark.parametrize("fn", ["durable", "durable-pyodide"])
def test_durable_flow_in_the_edge_runtime(edge_pgflow, fn):
    """Start a run in the function; its worker runs every step, with a retry and a route."""
    from stack import logs as container_logs

    edge_base, json, container = edge_pgflow

    def logs():
        """The Edge Runtime's log."""
        return container_logs(container)

    request = urllib.request.Request(
        f"{edge_base}/{fn}/flows",
        data=json.dumps({"id": 11, "suspicious": True}).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {SERVICE}", "x-cold": "1"},
    )
    try:
        response = urllib.request.urlopen(request, timeout=60)
    except urllib.error.HTTPError as error:
        pytest.fail(f"start failed: {error.code} {error.read()[:300]}\n{logs()}")
    with response:
        assert response.status == 202
        run_id = json.loads(response.read())["run_id"]
    state = None
    for _ in range(60):
        status_request = urllib.request.Request(
            f"{edge_base}/{fn}/flows/{run_id}",
            headers={"Authorization": f"Bearer {SERVICE}"},
        )
        with urllib.request.urlopen(status_request, timeout=30) as response:
            state = json.loads(response.read())
        if state["status"] != "started":
            break
        time.sleep(0.5)
    errors = {k: v["error"] for k, v in state["steps"].items() if v["error"]}
    assert state["status"] == "completed", f"{state['status']} {errors}\n{logs()}"
    assert state["output"] == {"review": {"decision": "refer", "id": 11}}
    assert state["steps"]["approve"]["status"] == "skipped"
