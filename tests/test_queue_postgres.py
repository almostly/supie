"""Queues on pgmq, against Supabase's Postgres and PostgREST (Docker)."""

import json
import time
import urllib.error
import urllib.request
from importlib import resources

import pytest
from stack import ANON, SERVICE, jwt, postgres_rest, proxy, psql

from supie.queue import Queue
from supie.testing import run

pytestmark = pytest.mark.postgres


@pytest.fixture(scope="module")
def rest(images):
    """Postgres with the queue migration applied twice, and PostgREST."""
    sql = resources.files("supie").joinpath("sql").joinpath("queue.sql").read_text()

    def setup(db):
        """Apply the migration twice: it's idempotent."""
        for _ in range(2):
            psql(db, sql, transaction=True)

    with postgres_rest(images, setup) as stack:
        yield stack


ENV = {"SUPABASE_SERVICE_ROLE_KEY": SERVICE}


def on_pgmq(base, coro):
    """Await something that talks to pgmq through the local PostgREST."""
    return run(coro, env=ENV, fetch=proxy(base), sleep_scale=0)


def test_send_work_retry_dead_and_replay(rest):
    """The whole life of messages on pgmq: handled, retried, dead, replayed."""
    base, db = rest
    queue = Queue("pg_scoring")
    seen = []

    @queue.consumer(batch=10, max_attempts=2, backoff=1)
    def handle(ctx, message):
        """Fail id 2 always, id 3 on its first attempt."""
        if message["id"] == 2 or (message["id"] == 3 and ctx.attempt == 1):
            raise ValueError(f"bad {message['id']}")
        seen.append((message["id"], ctx.attempt))

    async def go():
        """Send three, work until done."""
        ids = await queue.send_batch([{"id": 1}, {"id": 2}, {"id": 3}])
        counts = await queue.work(seconds=15, idle_polls=3)
        return ids, counts, await queue.stats()

    ids, counts, stats = on_pgmq(base, go())
    assert len(ids) == 3
    assert counts == {"handled": 2, "retried": 2, "dead": 1}
    assert sorted(seen) == [(1, 1), (3, 2)]
    assert stats["length"] == 0 and stats["dead"] == 1
    row = psql(db, "select message->>'id', attempts, error from supie_queue_dead")
    dead_id, attempts, error = row.split("|", 2)
    assert (dead_id, attempts) == ("2", "2") and "ValueError: bad 2" in error

    dead = psql(db, "select id from supie_queue_dead")
    replayed = on_pgmq(base, queue.replay(int(dead)))
    assert replayed is not None
    assert psql(db, "select count(*) from supie_queue_dead") == "0"
    assert psql(db, "select message->>'id' from pgmq.q_pg_scoring") == "2"


def test_delayed_send(rest):
    """A delayed message stays invisible until its delay passes."""
    base, _ = rest
    queue = Queue("pg_delayed")

    @queue.consumer()
    def handle(ctx, message):
        """Nothing to do."""

    async def go():
        """Send with a delay, then work right away."""
        await queue.send({"x": 1}, delay=30)
        return await queue.work(seconds=3, idle_polls=1), await queue.stats()

    counts, stats = on_pgmq(base, go())
    assert counts["handled"] == 0 and stats["length"] == 1


def test_only_the_service_role(rest):
    """anon and signed-in users can't call the queue functions or read dead letters."""
    base, _ = rest
    user = jwt({"role": "authenticated", "sub": "u1"})
    for token in (ANON, user):
        for path, body in [
            ("/rpc/supie_queue_send", b'{"queue": "x", "messages": [1]}'),
            ("/rpc/supie_queue_read", b'{"queue": "x"}'),
            ("/supie_queue_dead", None),
        ]:
            request = urllib.request.Request(
                base + path,
                data=body,
                method="POST" if body else "GET",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
            with pytest.raises(urllib.error.HTTPError) as denied:
                urllib.request.urlopen(request, timeout=10)
            assert denied.value.code in (401, 403, 404), path


@pytest.fixture(scope="module")
def edge_queue(rest, tmp_path_factory, images):
    """The queue function built for both runtimes, next to pgmq's PostgREST."""
    import shutil

    from stack import EDGE, edge_runtime

    from supie import build

    root = tmp_path_factory.mktemp("queue-edge")
    (root / "supabase").mkdir()
    (root / "supabase" / "config.toml").write_text('project_id = "q"\n')
    for name, queue_name in [
        ("queued", "edge_mpy_q"),
        ("queued-pyodide", "edge_pyodide_q"),
    ]:
        shutil.copytree(EDGE / "queued", root / "py" / name)
        main = root / "py" / name / "main.py"
        main.write_text(main.read_text().replace("edge_queue", queue_name))
    (root / "supie.toml").write_text(
        '[functions.queued-pyodide]\nruntime = "pyodide"\n'
    )
    project = build.load_project(root)
    for name in ("queued", "queued-pyodide"):
        build.build(project, project.functions[name])
    with edge_runtime(
        rest, images, root / "supabase" / "functions", "/queued/queue/stats"
    ) as served:
        yield served


@pytest.mark.edge
@pytest.mark.parametrize(
    "fn, queue_name", [("queued", "edge_mpy_q"), ("queued-pyodide", "edge_pyodide_q")]
)
def test_queue_in_the_edge_runtime(rest, edge_queue, fn, queue_name):
    """202 per application; background work handles them; failures become dead letters."""
    from stack import logs

    _, db = rest
    base, container = edge_queue
    for i in range(6):
        body = json.dumps({"id": i, "fail": i == 5}).encode()
        request = urllib.request.Request(
            f"{base}/{fn}/score",
            data=body,
            method="POST",
            headers={"Authorization": f"Bearer {ANON}"},
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            assert response.status == 202, logs(container)
    for _ in range(60):
        left = psql(db, f"select count(*) from pgmq.q_{queue_name}")
        dead = psql(
            db, f"select count(*) from supie_queue_dead where queue = '{queue_name}'"
        )
        if left == "0" and dead == "1":
            break
        time.sleep(0.5)
    assert (left, dead) == ("0", "1"), logs(container)
    error = psql(db, f"select error from supie_queue_dead where queue = '{queue_name}'")
    assert "application 5 is invalid" in error
    stats = urllib.request.Request(
        f"{base}/{fn}/queue/stats", headers={"Authorization": f"Bearer {SERVICE}"}
    )
    with urllib.request.urlopen(stats, timeout=30) as response:
        assert json.loads(response.read())["dead"] == 1
