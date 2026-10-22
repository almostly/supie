"""Queues under CPython, with messages in memory."""

import base64
import json
import time

import pytest

from supie import App, Response, sleep
from supie.queue import MemoryQueue, Queue
from supie.testing import TestClient, run


def token(claims):
    """A bearer token with these claims (the gateway would have verified it)."""
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return {"Authorization": f"Bearer x.{payload}.sig"}


SERVICE = token({"role": "service_role"})


def scoring_queue(seen, fail_until=0, always_fail=False):
    """A queue whose consumer records messages, failing as asked."""
    queue = Queue("scoring", MemoryQueue())

    @queue.consumer(batch=5, max_attempts=3, backoff=0)
    async def handle(ctx, message):
        """Fail the first attempts, or always."""
        if always_fail or ctx.attempt <= fail_until:
            raise RuntimeError(f"attempt {ctx.attempt} failed")
        seen.append((message["id"], ctx.attempt))

    return queue


def test_send_then_work():
    """Sent messages are handled and acknowledged."""
    seen = []
    queue = scoring_queue(seen)

    async def go():
        """Send three, then work."""
        ids = await queue.send_batch([{"id": 1}, {"id": 2}, {"id": 3}])
        counts = await queue.work(seconds=5)
        return ids, counts, await queue.stats()

    ids, counts, stats = run(go())
    assert ids == [1, 2, 3]
    assert counts == {"handled": 3, "retried": 0, "dead": 0}
    assert sorted(seen) == [(1, 1), (2, 1), (3, 1)]
    assert stats["length"] == 0


def test_retry_then_succeed():
    """A failing message comes back and succeeds on a later attempt."""
    seen = []
    queue = scoring_queue(seen, fail_until=2)

    async def go():
        """Send one and work until it's done."""
        await queue.send({"id": 7})
        return await queue.work(seconds=5, idle_polls=2)

    counts = run(go())
    assert counts == {"handled": 1, "retried": 2, "dead": 0}
    assert seen == [(7, 3)]


def test_dead_letter_and_replay():
    """After max_attempts a message is dead, with its error; replay sends it back."""
    seen = []
    queue = scoring_queue(seen, always_fail=True)
    backend = queue.backend
    assert isinstance(backend, MemoryQueue)

    async def go():
        """Send, exhaust the attempts, then replay."""
        await queue.send({"id": 9})
        counts = await queue.work(seconds=5, idle_polls=3)
        dead = dict(backend.dead)
        new_id = await queue.replay(next(iter(dead)))
        return counts, dead, new_id, await queue.stats()

    counts, dead, new_id, stats = run(go())
    assert counts == {"handled": 0, "retried": 2, "dead": 1}
    (letter,) = dead.values()
    assert letter["attempts"] == 3 and letter["message"] == {"id": 9}
    assert "RuntimeError: attempt 3 failed" in letter["error"]
    assert new_id is not None and stats == {"length": 1, "dead": 0, "total_sent": 2}


def test_backoff_delays_the_retry():
    """A retry stays hidden for the backoff, so a quick second read finds nothing."""
    queue = Queue("slow", MemoryQueue())
    attempts = []

    @queue.consumer(max_attempts=3, backoff=5)
    def handle(ctx, message):
        """Fail every time."""
        attempts.append(ctx.attempt)
        raise ValueError("no")

    async def go():
        """One work pass after the first failure."""
        await queue.send({"x": 1})
        return await queue.work(seconds=2, idle_polls=1)

    counts = run(go())
    assert counts == {"handled": 0, "retried": 1, "dead": 0} and attempts == [1]


def test_batch_runs_concurrently():
    """Messages in one read are handled at the same time."""
    queue = Queue("parallel", MemoryQueue())

    @queue.consumer(batch=10)
    async def handle(ctx, message):
        """Each message waits 0.2 s."""
        await sleep(0.2)

    async def go():
        """Five messages, one batch."""
        await queue.send_batch([{"i": i} for i in range(5)])
        started = time.perf_counter()
        counts = await queue.work(seconds=5)
        return counts, time.perf_counter() - started

    counts, elapsed = run(go())
    assert counts["handled"] == 5 and elapsed < 0.6


def test_timeout_counts_as_failure():
    """An async consumer that runs too long fails the attempt."""
    queue = Queue("timeouts", MemoryQueue())

    @queue.consumer(max_attempts=1, timeout=0.05)
    async def handle(ctx, message):
        """Too slow."""
        await sleep(1)

    async def go():
        """Send and work."""
        await queue.send({})
        return await queue.work(seconds=2)

    assert run(go())["dead"] == 1


def test_names_and_consumer_required():
    """Queue names follow pgmq's rules; work needs a consumer."""
    with pytest.raises(ValueError):
        Queue("Scoring-Queue")
    with pytest.raises(RuntimeError, match="no consumer"):
        run(Queue("idle", MemoryQueue()).work(seconds=1))


def test_send_from_handler_and_mounted_endpoints():
    """The README's pattern: 202 after send, then the work, stats and replay endpoints."""
    app = App()
    queue = Queue("scoring", MemoryQueue())
    handled = []

    @queue.consumer(max_attempts=1)
    async def handle(ctx, message):
        """Fail for id 0, record the rest."""
        if message["id"] == 0:
            raise ValueError("bad application")
        handled.append(message["id"])

    @app.post("/score")
    async def score(req):
        """Queue the application."""
        await queue.send(req.json())
        return Response({"queued": True}, status=202)

    queue.mount(app, "/queues/scoring")
    client = TestClient(app)
    assert client.post("/score", json={"id": 1}).status == 202
    client.post("/score", json={"id": 0})
    assert client.post("/queues/scoring/work").status == 403
    work = client.post("/queues/scoring/work?seconds=2", headers=SERVICE)
    assert work.json() == {"handled": 1, "retried": 0, "dead": 1}
    assert handled == [1]
    assert client.get("/queues/scoring/stats", headers=SERVICE).json()["dead"] == 1
    assert client.post("/queues/scoring/dead/1/replay", headers=SERVICE).json() == {
        "msg_id": 3
    }
    assert client.post("/queues/scoring/dead/99/replay", headers=SERVICE).status == 404
