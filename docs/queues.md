# Queues

For work that must not be lost when a request fails, accept it fast and handle it from a queue. A failed attempt comes back after a backoff, and a message that keeps failing is kept, with its error, instead of disappearing:

```python
from supie import App, Request, Response
from supie.queue import Queue

app = App()
queue = Queue("scoring")


@app.post("/score")
async def score(req: Request) -> Response:
    await queue.send(req.json())  # durable once this returns
    req.background(queue.work(req))  # optional: start right away
    return Response({"queued": True}, status=202)


@queue.consumer(batch=10, max_attempts=5)
async def handle(ctx, message):
    ...


queue.mount(app, "/queues/scoring")  # work, stats and replay endpoints
```

```bash
supie queue migration  # pgmq, the supie_queue_* functions and the dead-letter table
supabase db push
```

When you update supie, run `supie queue migration` again and redeploy the functions in the same release: the SQL functions and the Python that calls them change together.

- **Delivery.** At least once. A message stays hidden for `visibility` seconds while it's handled, so keep that above the slowest handling; if a worker dies mid-message, it comes back after that.
- **Retries.** A failed attempt (an exception, or `timeout` passed) comes back after about `backoff`, then twice that, and so on up to `max_backoff` (300 s), each delay shortened by up to half at random so retries after an outage don't all land at once. `ctx.attempt` says which attempt this is.
- **Dead letters.** After `max_attempts`, the message moves to `supie_queue_dead` with its last error and attempt count. `POST <prefix>/dead/<id>/replay` sends it back.
- **Who handles messages.** `queue.work()` handles messages until the queue is idle: call it in the background after sending, and from pg_cron through `POST <prefix>/work` so nothing waits for the next request. The endpoint's `?seconds=` (60 by default) is capped at `mount(..., max_seconds=140)`, under the free plan's 150 s wall-clock limit; raise it on paid plans. With a consumer `timeout`, `work` doesn't start a batch that could run past its seconds.
- **Batches.** The messages in one read are handled concurrently, and the handled ones are acknowledged in one call.

## Exactly-once results

A queue delivers each message at least once: nothing is lost once `send` returns, but a message can occasionally be handled twice (a worker finishes, then dies before acknowledging). Two tools make the result exactly once:

```python
await queue.send(application, key=f"application-{application['id']}")


@queue.consumer(max_attempts=5)
async def handle(ctx, message):
    await ctx.once(f"decide:{message['id']}", lambda: decide(message))
```

- **Send keys.** A message is enqueued once per key. A client that got no 202 can safely send again: the same key returns the first message's ID and enqueues nothing, even when the retries race each other. A message that goes to the dead letters takes its key along, so sending the key again enqueues it afresh, and replaying the dead letter then returns that message instead of adding a second.
- **`ctx.once(key, fn)`.** Runs `fn` once per key and stores its JSON result; any later delivery gets the stored result without running `fn`. While `fn` runs, the key holds a lease (`lease=`, five times the visibility by default; keep it above `fn`'s longest run), so a delivery arriving meanwhile comes back when the lease ends instead of running it in parallel, without using up an attempt. If `fn` raises or is cancelled, the lease is released and a retry runs it; a runner whose lease ran out can't release or finish the one that took over.
- **The one gap.** If a worker dies after `fn` finished but before its result was stored, the next delivery runs `fn` again. That can't be closed for effects outside the database (an email, a payment API), so give those their own idempotency key where the API offers one.

Tested with forced duplicates: a crash after the effect, a failed effect, concurrent deliveries of one effect, a message redelivered to a second worker mid-effect, and racing retries of one send, on pgmq and in the Edge Runtime on both runtimes. Keys and results stay in `supie_queue_keys` and `supie_queue_once`. `supie_queue_prune()` deletes those older than 7 days (pass `older_than`), and dead letters only when you pass `dead_older_than`. A key deduplicates sends for as long as it's kept: a send repeated after its key was pruned enqueues again. Schedule it with pg_cron:

```sql
select cron.schedule('supie-queue-prune', '0 3 * * *', 'select public.supie_queue_prune()');
```

Messages live in pgmq, the engine behind Supabase Queues. Like the pgflow bridge, supie reaches it through `public` functions only the service role can call, so you don't need to expose the `pgmq_public` schema. For tests, `Queue("scoring", MemoryQueue())` keeps messages in memory.
