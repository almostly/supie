"""Workflows under CPython, with checkpoints in memory."""

import base64
import json
import time

import pytest

from supie import App, sleep
from supie.flow import Flow, MemoryStore, StepFailed
from supie.testing import TestClient, run


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


def token(claims):
    """A bearer token with these claims (the gateway would have verified it)."""
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return {"Authorization": f"Bearer x.{payload}.sig"}


def mounted():
    """An app with the scoring flow mounted at /flows/scoring."""
    app = App()
    flow = scoring_flow()
    flow.mount(app, "/flows/scoring")
    return TestClient(app), flow


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
    assert client.get(path, headers=token({"role": "service_role"})).status == 200


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
    service = token({"role": "service_role"})
    response = client.post("/flows/scoring/resume-stale", headers=service)
    assert response.json() == {"resumed": [run_id]}
    assert flow.store.runs[run_id]["status"] == "succeeded"
