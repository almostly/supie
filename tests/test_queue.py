"""Queues under CPython, with messages in memory."""

import base64
import json
import time

import pytest

from supie import App, Response, sleep
from supie.queue import MemoryQueue, Queue
from supie.testing import FakeResponse, TestClient, run


def token(claims):
    """A bearer token with these claims (the gateway would have verified it)."""
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return {"Authorization": f"Bearer x.{payload}.sig"}


SERVICE = {"Authorization": "Bearer service-role-key"}  # DEFAULT_ENV's key


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
    forged = token({"role": "service_role"})
    assert client.post("/queues/scoring/work", headers=forged).status == 403
    assert client.get("/queues/scoring/stats", headers=forged).status == 403


def test_service_tokens_are_checked_with_supabase_auth():
    """Another service role token passes when Supabase Auth accepts it, and is
    remembered; one Auth rejects is refused.
    """
    app = App()
    Queue("auth_checked", MemoryQueue()).mount(app, "/q")
    genuine = token({"role": "service_role", "iat": 1})
    forged = token({"role": "service_role", "iat": 2})
    auth_calls = []

    def auth(call):
        """Supabase Auth: accepts only the genuine token."""
        auth_calls.append(call.headers["apikey"])
        ok = call.headers["apikey"] == genuine["Authorization"][7:]
        return FakeResponse(
            {"users": []} if ok else {"msg": "invalid JWT"}, 200 if ok else 401
        )

    client = TestClient(app, fetch=auth)
    assert client.get("/q/stats", headers=genuine).status == 200
    assert client.get("/q/stats", headers=genuine).status == 200
    assert client.get("/q/stats", headers=forged).status == 403
    assert len(auth_calls) == 2, "the genuine token is checked once"


def test_secret_api_keys_pass_the_service_check():
    """Any of the project's secret keys passes, in the apikey header or as a
    bearer token, compared locally; an unknown or publishable key doesn't.
    """
    app = App()
    Queue("secret_keyed", MemoryQueue()).mount(app, "/q")
    keys = '{"default": "sb_secret_default", "jobs": "sb_secret_jobs"}'
    client = TestClient(app, env={"SUPABASE_SECRET_KEYS": keys})
    assert client.get("/q/stats", headers={"apikey": "sb_secret_jobs"}).status == 200
    bearer = {"Authorization": "Bearer sb_secret_default"}
    assert client.get("/q/stats", headers=bearer).status == 200
    for key in ("sb_secret_other", "sb_publishable_default", "sb_secret_"):
        assert client.get("/q/stats", headers={"apikey": key}).status == 403
    assert client.calls == [], "secret keys are checked without calling Auth"

    single = TestClient(app, env={"SUPABASE_SECRET_KEY": "sb_secret_local"})
    assert single.get("/q/stats", headers={"apikey": "sb_secret_local"}).status == 200


def test_work_seconds_are_capped_and_checked():
    """The work endpoint caps ?seconds= at max_seconds and rejects a non-number."""
    app = App()
    queue = Queue("busy_worker", MemoryQueue())

    @queue.consumer(max_attempts=1000, backoff=0)
    async def handle(ctx, message):
        """Fail forever, so the queue is never idle."""
        raise RuntimeError("again")

    queue.mount(app, "/q", max_seconds=0.3)
    client = TestClient(app)
    run(queue.send({}))
    started = time.time()
    assert client.post("/q/work?seconds=1000", headers=SERVICE).status == 200
    assert time.time() - started < 5
    assert client.post("/q/work?seconds=soon", headers=SERVICE).status == 400


def test_handled_messages_are_acknowledged_per_batch():
    """One ack call for a batch's handled messages; failures retry on their own."""

    class Counting(MemoryQueue):
        """Record each ack call."""

        def __init__(self):
            """Start with no acks."""
            super().__init__()
            self.acks = []

        async def ack(self, msg_ids):
            """Record, then delete."""
            self.acks.append(list(msg_ids))
            await super().ack(msg_ids)

    backend = Counting()
    queue = Queue("acked", backend)

    @queue.consumer(batch=10, max_attempts=1)
    async def handle(ctx, message):
        """Fail message 2."""
        if message["n"] == 2:
            raise ValueError("bad")

    async def go():
        """Four messages in one batch."""
        await queue.send_batch([{"n": n} for n in range(4)])
        return await queue.work(seconds=5)

    assert run(go()) == {"handled": 3, "retried": 0, "dead": 1}
    assert backend.acks == [[1, 2, 4]]


def test_backoff_doubles_up_to_the_cap_with_jitter():
    """Each delay is the doubled backoff, capped, less up to half at random."""
    queue = Queue("backing_off", MemoryQueue())

    @queue.consumer(backoff=10, max_backoff=60)
    async def handle(ctx, message):
        """Unused."""

    async def delays(attempt):
        """Many draws for one attempt."""
        return {queue._backoff(attempt) for _ in range(200)}

    first, third, tenth = run(delays(1)), run(delays(3)), run(delays(10))
    assert min(first) >= 5 and max(first) <= 10
    assert min(third) >= 20 and max(third) <= 40
    assert min(tenth) >= 30 and max(tenth) <= 60 and len(tenth) > 5


def test_send_keys_enqueue_once():
    """A retried send with the same key returns the first ID and adds nothing."""
    queue = Queue("keyed", MemoryQueue())

    async def go():
        """Send keyed and unkeyed messages, then repeat the keyed ones."""
        first = await queue.send_batch(
            [{"a": 1}, {"a": 2}, {"a": 3}], keys=["k1", None, "k3"]
        )
        again = await queue.send({"a": 1}, key="k1")
        batch = await queue.send_batch([{"a": 3}, {"a": 4}], keys=["k3", "k4"])
        return first, again, batch, await queue.stats()

    first, again, batch, stats = run(go())
    assert first == [1, 2, 3] and again == 1 and batch == [3, 4]
    assert stats["length"] == 4
    with pytest.raises(ValueError, match="one entry per message"):
        run(queue.send_batch([{}, {}], keys=["only-one"]))


def test_a_dead_letter_takes_its_key_along():
    """A resend after the message died enqueues it again; replaying then adds nothing."""
    queue = Queue("keyed_dead", MemoryQueue())

    @queue.consumer(max_attempts=1)
    async def handle(ctx, message):
        """Always fail."""
        raise RuntimeError("down")

    async def go():
        """Send, let it die, resend with the key, then replay the dead letter."""
        first = await queue.send({"a": 1}, key="k")
        await queue.work(seconds=5, idle_polls=2)
        again = await queue.send({"a": 1}, key="k")
        replayed = await queue.replay(1)
        return first, again, replayed, await queue.stats()

    first, again, replayed, stats = run(go())
    assert again != first and replayed == again
    assert stats["length"] == 1 and stats["dead"] == 0


def test_once_survives_a_crash_after_the_effect():
    """The effect ran, then the handler crashed before ack: the redelivery doesn't repeat it."""
    queue = Queue("crashy", MemoryQueue())
    effects = []

    @queue.consumer(max_attempts=3, backoff=0)
    async def handle(ctx, message):
        """Send the email once, then crash on the first attempt."""

        async def send_email():
            """The effect."""
            effects.append(message["id"])
            return {"email": "sent", "to": message["id"]}

        result = await ctx.once(f"email:{message['id']}", send_email)
        assert result == {"email": "sent", "to": message["id"]}
        if ctx.attempt == 1:
            raise RuntimeError("worker died before ack")

    async def go():
        """One message, worked until done."""
        await queue.send({"id": 5})
        return await queue.work(seconds=5, idle_polls=2)

    assert run(go()) == {"handled": 1, "retried": 1, "dead": 0}
    assert effects == [5]


def test_once_retries_a_failed_effect():
    """An effect that raises releases its lease, so the retry runs it again."""
    queue = Queue("flaky_effect", MemoryQueue())
    attempts = []

    @queue.consumer(max_attempts=3, backoff=0)
    async def handle(ctx, message):
        """The effect fails the first time."""

        def charge():
            """The effect."""
            attempts.append(ctx.attempt)
            if ctx.attempt == 1:
                raise ConnectionError("payment API down")
            return "charged"

        assert await ctx.once("charge:1", charge) == "charged"

    async def go():
        """One message, worked until done."""
        await queue.send({})
        return await queue.work(seconds=5, idle_polls=2)

    assert run(go()) == {"handled": 1, "retried": 1, "dead": 0}
    assert attempts == [1, 2]


def test_once_makes_concurrent_deliveries_wait():
    """Two deliveries of the same effect at once: one runs it, the other waits
    for the lease without using up an attempt, then reuses the result.
    """
    queue = Queue("racing", MemoryQueue())
    effects = []
    results = []

    @queue.consumer(batch=10, max_attempts=1)
    async def handle(ctx, message):
        """Both messages share one effect key."""

        async def effect():
            """Slow enough for the other delivery to arrive meanwhile."""
            effects.append(ctx.msg_id)
            await sleep(0.3)
            return "done once"

        results.append(await ctx.once("shared", effect, lease=1))

    async def go():
        """Two messages in one batch, then the waiting one after the lease."""
        await queue.send_batch([{"n": 1}, {"n": 2}])
        first = await queue.work(seconds=5, idle_polls=3)
        await sleep(1.1)
        return first, await queue.work(seconds=5, idle_polls=3)

    first, second = run(go())
    assert first == {"handled": 1, "retried": 1, "dead": 0}
    assert second == {"handled": 1, "retried": 0, "dead": 0}
    assert len(effects) == 1 and results == ["done once", "done once"]


def test_once_doesnt_repeat_an_effect_whose_result_isnt_json():
    """The effect ran but its result can't be stored: the attempt fails, the retry doesn't rerun it."""
    queue = Queue("odd_result", MemoryQueue())
    effects = []
    results = []

    @queue.consumer(max_attempts=3, backoff=0)
    async def handle(ctx, message):
        """The effect returns something JSON can't hold."""

        def effect():
            """The effect."""
            effects.append(ctx.attempt)
            return object()

        results.append(await ctx.once("odd:1", effect))

    async def go():
        """One message, worked until done."""
        await queue.send({})
        return await queue.work(seconds=5, idle_polls=2)

    assert run(go()) == {"handled": 1, "retried": 1, "dead": 0}
    assert effects == [1] and results == [None]


def test_once_ignores_a_runner_whose_lease_ran_out():
    """A runner that lost its lease can't release or finish the one that took over."""
    backend = MemoryQueue()

    async def go():
        """A's lease runs out, B takes over, then A reports late."""
        a = await backend.once_begin("k", 0)
        b = await backend.once_begin("k", 60)
        await backend.once_release("k", a["token"])
        during = await backend.once_begin("k", 60)
        await backend.once_finish("k", a["token"], "from a")
        await backend.once_finish("k", b["token"], "from b")
        return during, await backend.once_begin("k", 60)

    during, after = run(go())
    assert during["state"] == "busy"
    assert after == {"state": "done", "result": "from b"}


def test_once_releases_the_lease_when_cancelled():
    """A handler cut off by the consumer timeout frees the key for the retry."""
    queue = Queue("slow_effect", MemoryQueue())
    attempts = []

    @queue.consumer(max_attempts=3, backoff=0, timeout=0.1)
    async def handle(ctx, message):
        """The first attempt hangs past the timeout."""

        async def effect():
            """The effect."""
            attempts.append(ctx.attempt)
            if ctx.attempt == 1:
                await sleep(5)
            return "ok"

        assert await ctx.once("slow:1", effect) == "ok"

    async def go():
        """One message, worked until done."""
        await queue.send({})
        return await queue.work(seconds=5, idle_polls=2)

    assert run(go()) == {"handled": 1, "retried": 1, "dead": 0}
    assert attempts == [1, 2]
