"""A queue in the edge function: accept with 202, handle in the background."""

from __future__ import annotations

from typing import Any

from supie import App, Request, Response
from supie.queue import Queue

app = App()
queue = Queue("edge_queue")


@queue.consumer(batch=10, max_attempts=2, backoff=1)
async def handle(ctx: Any, message: dict[str, Any]) -> None:
    """Fail messages marked to fail; the rest succeed."""
    if message.get("fail"):
        raise ValueError("application " + str(message["id"]) + " is invalid")


@app.post("/score")
async def score(req: Request) -> Response:
    """Queue the application and start working on the queue after the response."""
    msg_id = await queue.send(req.json())
    req.background(queue.work(req, seconds=20, idle_polls=3))
    return Response({"queued": True, "msg_id": msg_id}, status=202)


queue.mount(app, "/queue")
