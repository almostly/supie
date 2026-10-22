"""Queues: accept work fast, process it with retries, keep what keeps failing.

    queue = Queue("scoring")

    @app.post("/score")
    async def score(req):
        await queue.send(req.json())                 # durable once this returns
        return Response({"queued": True}, status=202)

    @queue.consumer(batch=10, max_attempts=5)        # run by queue.work()
    async def handle(ctx, message):
        ...

Messages live in pgmq (Supabase Queues), reached through the supie_queue_*
functions (`supie queue migration`). A message is delivered at least once: a
failed attempt comes back after a backoff, and after max_attempts it moves to
the supie_queue_dead table, with the error, until replayed. Work runs when
something calls queue.work(): pg_cron through the mounted work endpoint, or a
handler right after sending.

Like supie.app, this module runs under CPython, MicroPython and Pyodide.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from supie.app import (
    HTTPError,
    env,
    format_exception,
    gather,
    wait_for,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Protocol, TypeVar

    from supie.app import App, Request
    from supie.supabase import Supabase

    Handler = Callable[..., Any]
    F = TypeVar("F", bound=Callable[..., Any])

    class Backend(Protocol):
        """Where a queue's messages live."""

        async def send(self, messages: list[Any], delay: int) -> list[int]:
            """Enqueue messages after delay seconds; return their IDs."""
            ...

        async def read(
            self, visibility: int, qty: int, poll_seconds: int
        ) -> list[dict[str, Any]]:
            """Take up to qty visible messages, hiding them for visibility seconds."""
            ...

        async def ack(self, msg_id: int) -> None:
            """Delete a handled message."""
            ...

        async def retry(self, msg_id: int, delay: int) -> None:
            """Make a message visible again after delay seconds."""
            ...

        async def kill(self, msg_id: int, error: str) -> int:
            """Move a message to the dead letters; return its dead-letter ID."""
            ...

        async def replay(self, dead_id: int) -> int | None:
            """Send a dead letter back to the queue; return its new ID."""
            ...

        async def stats(self) -> dict[str, Any]:
            """Queue length, oldest message age and dead-letter count."""
            ...


def _check_name(name: str) -> None:
    """pgmq queue names: lower-case letters, digits and underscores, 47 at most."""
    ok = 0 < len(name) <= 47 and (name[0].isalpha() and name[0].lower() == name[0])
    for char in name:
        if not (
            char == "_" or char.isdigit() or (char.isalpha() and char.lower() == char)
        ):
            ok = False
    if not ok:
        raise ValueError(
            "queue name " + repr(name) + " isn't valid: use lower-case letters, "
            "digits and underscores, starting with a letter"
        )


class QueueContext:
    """What a consumer can use: the message's ID and attempt, and Supabase."""

    def __init__(
        self, queue: Queue, msg_id: int, attempt: int, request: Request | None
    ) -> None:
        """Store the message this context belongs to."""
        self.queue = queue
        self.msg_id = msg_id
        self.attempt = attempt
        self.request = request

    @property
    def supabase_admin(self) -> Supabase:
        """A client with the service role key (consumers run as the service)."""
        from supie.supabase import Supabase

        key = env["SUPABASE_SERVICE_ROLE_KEY"]
        return Supabase(env["SUPABASE_URL"], key, key)

    async def invoke(
        self, name: str, body: Any = None, headers: dict[str, str] | None = None
    ) -> Any:
        """Call another Edge Function as the service."""
        return await self.supabase_admin.functions.invoke(name, body, headers)


class Queue:
    """A named pgmq queue, and the consumer that handles its messages."""

    def __init__(self, name: str, backend: Backend | None = None) -> None:
        """Name the queue; backend defaults to pgmq through supie_queue_*."""
        _check_name(name)
        self.name = name
        self.backend = backend
        self.handler: Handler | None = None
        self.batch = 10
        self.max_attempts = 5
        self.backoff = 1.0
        self.visibility = 30
        self.timeout: float | None = None

    def _backend(self) -> Backend:
        """The backend, creating the Supabase one on first use."""
        if self.backend is None:
            self.backend = SupabaseQueue(self.name)
        return self.backend

    async def send(self, message: Any, delay: float = 0) -> int:
        """Enqueue one JSON message, visible after delay seconds; return its ID."""
        return (await self.send_batch([message], delay))[0]

    async def send_batch(self, messages: list[Any], delay: float = 0) -> list[int]:
        """Enqueue several JSON messages at once; return their IDs."""
        payload = [json.loads(json.dumps(m)) for m in messages]
        return await self._backend().send(payload, int(delay))

    def consumer(
        self,
        batch: int = 10,
        max_attempts: int = 5,
        backoff: float = 1.0,
        visibility: int = 30,
        timeout: float | None = None,
    ) -> Callable[[F], F]:
        """Register the function that handles each message: fn(ctx, message).

        batch: messages taken per read, handled concurrently. A failed attempt
        comes back after backoff, 2*backoff, ... seconds; after max_attempts the
        message goes to the dead letters. visibility: seconds a message stays
        hidden while handled, so keep it above the slowest handling. timeout:
        seconds before an async handler counts as failed.
        """

        def register(fn: F) -> F:
            """Keep the handler and its settings; return it unchanged."""
            self.handler = fn
            self.batch = batch
            self.max_attempts = max_attempts
            self.backoff = backoff
            self.visibility = visibility
            self.timeout = timeout
            return fn

        return register

    async def work(
        self, request: Request | None = None, seconds: float = 60, idle_polls: int = 1
    ) -> dict[str, int]:
        """Handle messages until the queue is idle or seconds have passed.

        Returns how many messages were handled, retried and sent to the dead
        letters. Each message's CPU counts against this invocation's budget,
        so a worker stops early rather than run forever.
        """
        if self.handler is None:
            raise RuntimeError(self.name + " has no consumer: use @queue.consumer()")
        backend = self._backend()
        counts = {"handled": 0, "retried": 0, "dead": 0}
        idle = 0
        deadline = _now() + seconds
        while idle < idle_polls and _now() < deadline:
            messages = await backend.read(self.visibility, self.batch, 1)
            if not messages:
                idle += 1
                continue
            idle = 0
            outcomes = await gather(
                *[self._handle(backend, m, request) for m in messages]
            )
            for outcome in outcomes:
                counts[outcome] += 1
        return counts

    async def _handle(
        self, backend: Backend, record: dict[str, Any], request: Request | None
    ) -> str:
        """Handle one message; ack it, retry it or move it to the dead letters."""
        handler = self.handler
        assert handler is not None
        attempt = int(record["attempt"])
        ctx = QueueContext(self, int(record["msg_id"]), attempt, request)
        try:
            result = handler(ctx, record["message"])
            if hasattr(result, "send"):
                if self.timeout:
                    await wait_for(result, self.timeout)
                else:
                    await result
        except Exception as error:
            if attempt >= self.max_attempts:
                await backend.kill(ctx.msg_id, format_exception(error)[-4000:])
                return "dead"
            await backend.retry(ctx.msg_id, int(self.backoff * 2 ** (attempt - 1)))
            return "retried"
        await backend.ack(ctx.msg_id)
        return "handled"

    async def stats(self) -> dict[str, Any]:
        """Queue length, oldest message age in seconds and dead-letter count."""
        return await self._backend().stats()

    async def replay(self, dead_id: int) -> int | None:
        """Send a dead letter back to the queue; return its new ID, or None."""
        return await self._backend().replay(dead_id)

    def mount(self, app: App, prefix: str) -> Queue:
        """Add service-role endpoints: POST prefix/work, GET prefix/stats and
        POST prefix/dead/<id>/replay. Call the work endpoint from pg_cron.
        """
        queue = self

        def service_only(req: Request) -> None:
            """Refuse anyone but the service role."""
            if (req.user or {}).get("role") != "service_role":
                raise HTTPError(403, "needs the service role")

        @app.post(prefix + "/work")
        async def work(req: Request) -> dict[str, int]:
            """Handle waiting messages."""
            service_only(req)
            return await queue.work(req, seconds=int(req.query.get("seconds", "60")))

        @app.get(prefix + "/stats")
        async def stats(req: Request) -> dict[str, Any]:
            """Queue length and dead letters."""
            service_only(req)
            return await queue.stats()

        @app.post(prefix + "/dead/{dead_id:int}/replay")
        async def replay(req: Request, dead_id: int) -> dict[str, Any]:
            """Send a dead letter back to the queue."""
            service_only(req)
            msg_id = await queue.replay(dead_id)
            if msg_id is None:
                raise HTTPError(404, "no such dead letter")
            return {"msg_id": msg_id}

        return queue


def _now() -> float:
    """Seconds since an arbitrary point, for deadlines and memory visibility."""
    import time

    if hasattr(time, "ticks_ms"):
        return getattr(time, "ticks_ms")() / 1000
    return time.time()


class SupabaseQueue:
    """A pgmq queue reached through the supie_queue_* functions, as the service role."""

    def __init__(self, name: str, client: Supabase | None = None) -> None:
        """Use this client, or a service role client from the environment."""
        self.name = name
        self.client = client
        self._created = False

    async def _db(self) -> Supabase:
        """The client, and the queue created once per worker."""
        if self.client is None:
            from supie.supabase import Supabase

            key = env["SUPABASE_SERVICE_ROLE_KEY"]
            self.client = Supabase(env["SUPABASE_URL"], key, key)
        if not self._created:
            await self.client.rpc("supie_queue_create", {"queue": self.name})
            self._created = True
        return self.client

    async def send(self, messages: list[Any], delay: int) -> list[int]:
        """Enqueue messages."""
        db = await self._db()
        result = await db.rpc(
            "supie_queue_send",
            {"queue": self.name, "messages": messages, "delay": delay},
        )
        return list(result.data or [])

    async def read(
        self, visibility: int, qty: int, poll_seconds: int
    ) -> list[dict[str, Any]]:
        """Take visible messages."""
        db = await self._db()
        result = await db.rpc(
            "supie_queue_read",
            {
                "queue": self.name,
                "visibility": visibility,
                "qty": qty,
                "max_poll_seconds": poll_seconds,
            },
        )
        return list(result.data or [])

    async def ack(self, msg_id: int) -> None:
        """Delete a handled message."""
        db = await self._db()
        await db.rpc("supie_queue_ack", {"queue": self.name, "msg_id": msg_id})

    async def retry(self, msg_id: int, delay: int) -> None:
        """Make a message visible again later."""
        db = await self._db()
        await db.rpc(
            "supie_queue_retry", {"queue": self.name, "msg_id": msg_id, "delay": delay}
        )

    async def kill(self, msg_id: int, error: str) -> int:
        """Move a message to the dead letters."""
        db = await self._db()
        result = await db.rpc(
            "supie_queue_kill", {"queue": self.name, "msg_id": msg_id, "error": error}
        )
        return int(result.data)

    async def replay(self, dead_id: int) -> int | None:
        """Send a dead letter back."""
        db = await self._db()
        result = await db.rpc("supie_queue_replay", {"dead_id": dead_id})
        return None if result.data is None else int(result.data)

    async def stats(self) -> dict[str, Any]:
        """Queue metrics."""
        db = await self._db()
        result = await db.rpc("supie_queue_stats", {"queue": self.name})
        return dict(result.data or {})


class MemoryQueue:
    """A queue in memory, for tests: same behavior, no database."""

    def __init__(self) -> None:
        """Start empty."""
        self.messages: dict[int, dict[str, Any]] = {}
        self.dead: dict[int, dict[str, Any]] = {}
        self._next = 0
        self._next_dead = 0
        self.total_sent = 0

    async def send(self, messages: list[Any], delay: int) -> list[int]:
        """Enqueue messages."""
        ids = []
        for message in messages:
            self._next += 1
            self.messages[self._next] = {
                "message": json.loads(json.dumps(message)),
                "attempt": 0,
                "visible_at": _now() + delay,
                "enqueued_at": _now(),
            }
            ids.append(self._next)
        self.total_sent += len(ids)
        return ids

    async def read(
        self, visibility: int, qty: int, poll_seconds: int
    ) -> list[dict[str, Any]]:
        """Take visible messages, oldest first."""
        now = _now()
        out = []
        for msg_id in sorted(self.messages):
            if len(out) >= qty:
                break
            entry = self.messages[msg_id]
            if entry["visible_at"] <= now:
                entry["attempt"] += 1
                entry["visible_at"] = now + visibility
                out.append(
                    {
                        "msg_id": msg_id,
                        "attempt": entry["attempt"],
                        "message": entry["message"],
                    }
                )
        return out

    async def ack(self, msg_id: int) -> None:
        """Delete a handled message."""
        self.messages.pop(msg_id, None)

    async def retry(self, msg_id: int, delay: int) -> None:
        """Make a message visible again later."""
        self.messages[msg_id]["visible_at"] = _now() + delay

    async def kill(self, msg_id: int, error: str) -> int:
        """Move a message to the dead letters."""
        entry = self.messages.pop(msg_id)
        self._next_dead += 1
        self.dead[self._next_dead] = {
            "msg_id": msg_id,
            "message": entry["message"],
            "error": error,
            "attempts": entry["attempt"],
        }
        return self._next_dead

    async def replay(self, dead_id: int) -> int | None:
        """Send a dead letter back."""
        entry = self.dead.pop(dead_id, None)
        if entry is None:
            return None
        return (await self.send([entry["message"]], 0))[0]

    async def stats(self) -> dict[str, Any]:
        """Queue metrics."""
        return {
            "length": len(self.messages),
            "dead": len(self.dead),
            "total_sent": self.total_sent,
        }
