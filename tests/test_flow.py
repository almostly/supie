"""Workflows under CPython, with checkpoints in memory."""

import base64
import json
import time

import pytest

from supie import App, sleep
from supie.flow import Flow, MemoryStore, StepFailed
from supie.testing import FakeResponse, TestClient, run


def scoring_flow(store=None, calls=None):
    """A credit decision: two parallel checks, a score, then a decision or a branch."""
    calls = [] if calls is None else calls
    flow = Flow("scoring", store or MemoryStore())

    @flow.step()
    async def bureau(ctx, application):
        """A slow check."""
        calls.append("bureau")
        await sleep(0.1)
        return {"score": 700 if application["id"] != 13 else 500}

    @flow.step()
    async def fraud(ctx, application):
        """Another slow check, in parallel with bureau."""
        calls.append("fraud")
        await sleep(0.1)
        return {"flag": application.get("suspicious", False)}

    @flow.step(depends_on=[bureau, fraud])
    def score(ctx, application, bureau, fraud):
        """Combine the checks."""
        calls.append("score")
        return {"pd": 0.02 if bureau["score"] >= 600 else 0.3, "fraud": fraud["flag"]}

    @flow.step(depends_on=[score])
    def decide(ctx, application, score):
        """Approve, decline, or hand over to manual review."""
        calls.append("decide")
        if score["fraud"]:
            return ctx.goto(manual_review)
        return {"decision": "approve" if score["pd"] < 0.1 else "decline"}

    @flow.branch(depends_on=[score])
    def manual_review(ctx, application, score):
        """Only runs when decide goes to it."""
        calls.append("manual_review")
        return {"decision": "refer", "pd": score["pd"]}

    return flow


def test_runs_steps_in_dependency_order_with_parallel_checks():
    """Independent steps run concurrently; the last step's output is the result."""
    calls = []
    flow = scoring_flow(calls=calls)
    started = time.perf_counter()
    result = run(flow.run({"id": 1}))
    assert time.perf_counter() - started < 0.18  # bureau and fraud overlapped
    assert result.status == "succeeded"
    assert result.output == {"decision": "approve"}
    assert sorted(calls[:2]) == ["bureau", "fraud"] and calls[2:] == ["score", "decide"]
    assert "manual_review" not in result.steps


def test_goto_runs_the_branch_and_uses_its_output():
    """A goto hands the step's result to the branch."""
    flow = scoring_flow()
    result = run(flow.run({"id": 2, "suspicious": True}))
    assert result.output == {"decision": "refer", "pd": 0.02}
    rows = flow.store.runs[result.run_id]["step_rows"]
    assert rows["decide"]["goto"] == "manual_review"
    assert rows["manual_review"]["output"] == {"decision": "refer", "pd": 0.02}


def test_resume_skips_saved_steps():
    """Running a saved run ID again only runs what's left, with the saved input."""
    calls = []
    store = MemoryStore()
    flow = scoring_flow(store, calls)
    first = run(flow.run({"id": 1}))
    store.runs[first.run_id]["status"] = "running"
    del store.runs[first.run_id]["step_rows"]["decide"]
    calls.clear()
    again = run(flow.run(None, run_id=first.run_id))
    assert calls == ["decide"]
    assert again.output == {"decision": "approve"}


def test_finished_run_is_not_repeated():
    """A succeeded run returns its saved output without running anything."""
    calls = []
    flow = scoring_flow(calls=calls)
    first = run(flow.run({"id": 1}))
    calls.clear()
    assert run(flow.run(None, run_id=first.run_id)).output == first.output
    assert calls == []


def test_retries_then_succeeds():
    """A failing step is retried with backoff, up to retries extra attempts."""
    attempts = []
    flow = Flow("flaky", MemoryStore())

    @flow.step(retries=2, backoff=10)
    def call_bureau(ctx, input):
        """Fail twice, then succeed."""
        attempts.append(ctx.attempt)
        if ctx.attempt < 3:
            raise ConnectionError("bureau down")
        return "ok"

    result = run(flow.run({}), sleep_scale=0)
    assert result.output == "ok" and attempts == [1, 2, 3]
    assert isinstance(flow.store, MemoryStore)
    assert flow.store.runs[result.run_id]["step_rows"]["call_bureau"]["attempts"] == 3


def test_failure_is_saved_and_raised():
    """When retries run out, the step and the run are saved as failed."""
    store = MemoryStore()
    flow = Flow("broken", store)

    @flow.step(retries=1)
    def explode(ctx, input):
        """Always fail."""
        raise ValueError("bad input")

    with pytest.raises(StepFailed) as failure:
        run(flow.run({"x": 1}), sleep_scale=0)
    assert failure.value.step == "explode" and failure.value.attempts == 2
    (saved,) = store.runs.values()
    assert saved["status"] == "failed"
    assert "ValueError: bad input" in saved["step_rows"]["explode"]["error"]


def test_timeout():
    """An async step that takes too long fails."""
    flow = Flow("slow", MemoryStore())

    @flow.step(timeout=0.05)
    async def wait(ctx, input):
        """Take longer than the timeout."""
        await sleep(1)

    with pytest.raises(StepFailed, match="timed out"):
        run(flow.run({}))


def test_several_ends_give_a_dict():
    """With more than one final step, the output maps each to its result."""
    flow = Flow("fanout", MemoryStore())

    @flow.step()
    def a(ctx, input):
        """One end."""
        return 1

    @flow.step()
    def b(ctx, input):
        """Another end."""
        return 2

    assert run(flow.run({})).output == {"a": 1, "b": 2}


def test_registration_rules():
    """Unknown dependencies, steps depending on branches and bad gotos are refused."""
    flow = Flow("rules", MemoryStore())

    def stranger(ctx, input):
        """Not registered."""

    with pytest.raises(ValueError):
        flow.step(depends_on=[stranger])(lambda ctx, input: None)

    @flow.branch()
    def side(ctx, input):
        """A branch."""

    with pytest.raises(ValueError, match="only branches"):
        flow.step(depends_on=[side])(lambda ctx, input, side: None)

    @flow.step()
    def jump(ctx, input):
        """Go to something that isn't a branch."""
        return ctx.goto(jump)

    with pytest.raises(StepFailed, match="goto needs"):
        run(flow.run({}))


def test_outputs_must_be_json():
    """Checkpoints are JSON, so a step can't return arbitrary objects."""
    flow = Flow("odd", MemoryStore())

    @flow.step()
    def weird(ctx, input):
        """Return something JSON can't hold."""
        return {1, 2}

    with pytest.raises(TypeError):
        run(flow.run({}))


SERVICE = {"Authorization": "Bearer service-role-key"}  # DEFAULT_ENV's key


def token(claims, signature="sig"):
    """A bearer token with these claims; the fake Auth accepts the "sig" signature."""
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return {"Authorization": f"Bearer x.{payload}.{signature}"}


def auth(call):
    """Supabase Auth: loads the user of a token signed "sig", refuses the rest."""
    if not call.url.endswith("/auth/v1/user"):
        return FakeResponse("", 404)
    bearer = call.headers["Authorization"][7:]
    if not bearer.endswith(".sig"):
        return FakeResponse({"msg": "invalid JWT"}, 401)
    part = bearer.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    return FakeResponse({"id": claims["sub"]})


def mounted(fetch=auth, env=None):
    """An app with the scoring flow mounted at /flows/scoring."""
    app = App()
    flow = scoring_flow()
    flow.mount(app, "/flows/scoring")
    return TestClient(app, fetch=fetch, env=env), flow


def test_mount_start_and_status():
    """POST starts a run (202), it finishes in the background, GET reads it."""
    client, flow = mounted()
    started = client.post("/flows/scoring", json={"id": 1})
    assert started.status == 202
    run_id = started.json()["run_id"]
    assert started.headers["location"] == f"/flows/scoring/{run_id}"
    status = client.get(f"/flows/scoring/{run_id}").json()
    assert status["status"] == "succeeded"
    assert status["output"] == {"decision": "approve"}
    assert set(status["steps"]) == {"bureau", "fraud", "score", "decide"}
    assert client.get("/flows/scoring/nope").status == 404


def test_mount_runs_are_private_to_their_user():
    """A signed-in user's run is invisible to other users, visible to the service role."""
    client, _ = mounted()
    alice = token({"role": "authenticated", "sub": "alice"})
    run_id = client.post("/flows/scoring", json={"id": 1}, headers=alice).json()[
        "run_id"
    ]
    path = f"/flows/scoring/{run_id}"
    assert client.get(path, headers=alice).status == 200
    assert (
        client.get(path, headers=token({"role": "authenticated", "sub": "bob"})).status
        == 404
    )
    assert client.get(path, headers=SERVICE).status == 200


def test_claiming_the_service_role_is_not_enough():
    """A token that only claims the service role, as anyone can send when
    verify_jwt is off, can't read others' runs or call service endpoints.
    """
    client, _ = mounted()
    alice = token({"role": "authenticated", "sub": "alice"})
    run_id = client.post("/flows/scoring", json={"id": 1}, headers=alice).json()[
        "run_id"
    ]
    forged = token({"role": "service_role"}, signature="forged")
    assert client.get(f"/flows/scoring/{run_id}", headers=forged).status == 404
    assert client.post("/flows/scoring/resume-stale", headers=forged).status == 403


def test_a_forged_user_token_is_refused():
    """A token claiming another user's ID, refused by Auth, can't read or
    resume their run, and starting a run with one records no owner: it's 401.
    """
    client, flow = mounted()
    alice = token({"role": "authenticated", "sub": "alice"})
    run_id = client.post("/flows/scoring", json={"id": 1}, headers=alice).json()[
        "run_id"
    ]
    as_alice = token({"role": "authenticated", "sub": "alice"}, signature="forged")
    assert client.get(f"/flows/scoring/{run_id}", headers=as_alice).status == 401
    assert (
        client.post(f"/flows/scoring/{run_id}/resume", headers=as_alice).status == 401
    )
    before = set(flow.store.runs)
    assert client.post("/flows/scoring", json={"id": 2}, headers=as_alice).status == 401
    assert set(flow.store.runs) == before


def test_starting_a_run_fails_closed_when_auth_cant_answer():
    """With Auth unreachable, or no key to call it with, a signed-in start
    answers 503 and creates no run; a request without a token still starts one.
    """
    alice = token({"role": "authenticated", "sub": "alice"})

    def down(call):
        """Auth is unreachable."""
        raise ConnectionError("auth is down")

    for client, flow in (mounted(fetch=down), mounted(env={"SUPABASE_ANON_KEY": ""})):
        assert (
            client.post("/flows/scoring", json={"id": 1}, headers=alice).status == 503
        )
        assert flow.store.runs == {}
        assert client.post("/flows/scoring", json={"id": 1}).status == 202


def test_number_parameters_are_checked_and_capped():
    """The durable work endpoint caps ?seconds= and answers 400 for a value
    that isn't a finite number, as does resume-stale's ?idle=.
    """
    app = App()
    flow = scoring_flow()
    asked = []

    async def work(request=None, seconds=60.0, idle_polls=3):
        """Record the seconds instead of reaching pgflow."""
        asked.append(seconds)
        return 0

    flow.work = work
    flow.mount(app, "/durable", durable=True, max_seconds=30)
    scoring_flow().mount(app, "/inline")
    client = TestClient(app)
    for bad in ("abc", "nan", "inf"):
        assert (
            client.post(f"/durable/work?seconds={bad}", headers=SERVICE).status == 400
        )
        assert (
            client.post(f"/inline/resume-stale?idle={bad}", headers=SERVICE).status
            == 400
        )
    assert client.post("/durable/work?seconds=1000", headers=SERVICE).json() == {
        "ran": 0
    }
    assert client.post("/durable/work?seconds=-5", headers=SERVICE).json() == {"ran": 0}
    assert asked == [30, 0]


def test_mount_resume_and_resume_stale():
    """resume continues a run; resume-stale needs the service role."""
    client, flow = mounted()
    run_id = client.post("/flows/scoring", json={"id": 1}).json()["run_id"]
    saved = flow.store.runs[run_id]
    saved["status"] = "running"
    del saved["step_rows"]["decide"]
    assert client.post(f"/flows/scoring/{run_id}/resume").status == 202
    assert flow.store.runs[run_id]["status"] == "succeeded"

    assert client.post("/flows/scoring/resume-stale").status == 403
    saved["status"] = "running"
    del saved["step_rows"]["decide"]
    response = client.post("/flows/scoring/resume-stale", headers=SERVICE)
    assert response.json() == {"resumed": [run_id]}
    assert flow.store.runs[run_id]["status"] == "succeeded"


def test_contains_matches_postgres_semantics():
    """when/unless use jsonb containment: nested objects, arrays, no bool/int mixing."""
    from supie.flow import contains

    assert contains({"a": {"b": 1, "c": 2}}, {"a": {"b": 1}})
    assert not contains({"a": {"b": 1}}, {"a": {"b": 2}})
    assert contains({"tags": ["x", "y"]}, {"tags": ["y"]})
    assert not contains({"tags": ["x"]}, {"tags": ["y"]})
    assert not contains({"flag": 1}, {"flag": True})
    assert contains({"flag": True}, {"flag": True})
    assert not contains("text", {"a": 1})


def routing_flow(calls):
    """Score, decide a route, then approve or review depending on it."""
    flow = Flow("routing", MemoryStore())

    @flow.step()
    def score(ctx, application):
        """A PD from the input."""
        calls.append("score")
        return {"pd": application["pd"]}

    @flow.step(depends_on=[score])
    def decide(ctx, application, score):
        """Pick a route."""
        calls.append("decide")
        return {"route": "review" if score["pd"] > 0.1 else "approve"}

    @flow.step(depends_on=[decide], when={"decide": {"route": "approve"}})
    def approve(ctx, application, decide):
        """Runs only on the approve route."""
        calls.append("approve")
        return {"decision": "approve"}

    @flow.step(depends_on=[decide], when={"decide": {"route": "review"}})
    def review(ctx, application, decide):
        """Runs only on the review route."""
        calls.append("review")
        return {"decision": "refer"}

    @flow.step(unless={"test": True})
    def notify(ctx, application):
        """A root step skipped for test applications."""
        calls.append("notify")
        return "sent"

    return flow


def test_when_routes_and_skips():
    """Only the matching route runs; the result holds the final steps that ran."""
    calls = []
    flow = routing_flow(calls)
    result = run(flow.run({"pd": 0.3, "test": True}))
    assert sorted(calls) == ["decide", "review", "score"]
    assert result.output == {"review": {"decision": "refer"}}
    assert "approve" not in result.steps
    rows = flow.store.runs[result.run_id]["step_rows"]
    assert (
        rows["approve"]["status"] == "skipped" and rows["notify"]["status"] == "skipped"
    )

    calls.clear()
    result = run(flow.run({"pd": 0.01}))
    assert result.output == {"approve": {"decision": "approve"}, "notify": "sent"}


def test_dependents_of_skipped_steps_get_none():
    """A step after a skipped one still runs, with None for it."""
    flow = Flow("after-skip", MemoryStore())

    @flow.step(when={"go": True})
    def maybe(ctx, input):
        """Skipped unless go is true."""
        return "ran"

    @flow.step(depends_on=[maybe])
    def after(ctx, input, maybe):
        """Reports what it got."""
        return {"maybe": maybe}

    assert run(flow.run({"go": False})).output == {"maybe": None}
    assert run(flow.run({"go": True})).output == {"maybe": "ran"}


def test_shape_for_pgflow():
    """A flow compiles to pgflow's shape; goto branches and bad names are refused."""
    flow = routing_flow([])
    flow.name = "routing"
    shape = flow.shape()
    review = next(s for s in shape["steps"] if s["slug"] == "review")
    assert review["dependencies"] == ["decide"]
    assert review["requiredInputPattern"] == {
        "defined": True,
        "value": {"decide": {"route": "review"}},
    }
    assert review["forbiddenInputPattern"] == {"defined": False}
    notify = next(s for s in shape["steps"] if s["slug"] == "notify")
    assert notify["forbiddenInputPattern"] == {"defined": True, "value": {"test": True}}
    assert review["options"]["maxAttempts"] == 1

    with pytest.raises(ValueError, match="branch"):
        scoring_flow().shape()
    with pytest.raises(ValueError, match="isn't valid in pgflow"):
        Flow("scoring-flow").shape()
