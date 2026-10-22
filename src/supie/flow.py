"""Workflows: steps with dependencies, retries, timeouts and branches, checkpointed per run.

    scoring = Flow("scoring")

    @scoring.step()
    async def bureau(ctx, application):
        return await ctx.invoke("bureau-fetch", application)

    @scoring.step(depends_on=[bureau], retries=2, timeout=5)
    def score(ctx, application, bureau):
        return {"pd": model(application, bureau)}

    @scoring.step(depends_on=[score])
    def decide(ctx, application, score):
        if score["pd"] > 0.2:
            return ctx.goto(manual_review)
        return {"decision": "approve"}

    @scoring.branch(depends_on=[score])
    async def manual_review(ctx, application, score):
        return {"decision": "refer"}

Each step gets the flow's input and its dependencies' outputs. Steps whose
dependencies are done run concurrently. A branch runs only when a step returns
ctx.goto(branch), and its output becomes that step's output. Every finished step
is saved, so running the same run ID again resumes after the last saved step.

Like supie.app, this module runs under CPython, MicroPython and Pyodide.
"""

import json

from supie.app import (
    HTTPError,
    Response,
    env,
    format_exception,
    gather,
    new_id,
    sleep,
    wait_for,
)


class StepFailed(Exception):
    """A step raised on its last attempt; the run is marked failed."""

    def __init__(self, step, error, attempts):
        """Store the step's name, the error and how many attempts were made."""
        super().__init__(
            step + " failed after " + str(attempts) + " attempt(s): " + repr(error)
        )
        self.step = step
        self.error = error
        self.attempts = attempts


class Goto:
    """Returned by ctx.goto: hand this step's result to a branch."""

    def __init__(self, step):
        """Keep the branch to run."""
        self.step = step


class Step:
    """A registered step: the function and how to run it."""

    def __init__(self, fn, name, depends_on, retries, timeout, backoff, branch):
        """Store the step's function, settings and the names it depends on."""
        self.fn = fn
        self.name = name
        self.depends_on = depends_on
        self.retries = retries
        self.timeout = timeout
        self.backoff = backoff
        self.branch = branch


class Context:
    """What a step can use: the run, the attempt, Supabase and goto."""

    def __init__(self, flow, run_id, step, attempt, request):
        """Store the run and step this context belongs to."""
        self.flow = flow
        self.run_id = run_id
        self.step = step
        self.attempt = attempt
        self.request = request

    @property
    def supabase(self):
        """A client acting as the caller when the run came from a request, else the service role."""
        if self.request is not None:
            return self.request.supabase
        return self.supabase_admin

    @property
    def supabase_admin(self):
        """A client with the service role key."""
        from supie.supabase import Supabase

        key = env["SUPABASE_SERVICE_ROLE_KEY"]
        return Supabase(env["SUPABASE_URL"], key, key)

    async def invoke(self, name, body=None, headers=None):
        """Call another Edge Function and return its parsed response."""
        return await self.supabase.functions.invoke(name, body, headers)

    def goto(self, step):
        """Return this from a step to run a branch; the branch's output becomes the step's."""
        target = self.flow._by_fn.get(step)
        if target is None or not target.branch:
            raise ValueError("goto needs a step registered with @flow.branch()")
        return Goto(target)


class Run:
    """A finished run: its ID, status, output and every step's output."""

    def __init__(self, run_id, status, output, steps):
        """Store the outcome of a run."""
        self.run_id = run_id
        self.status = status
        self.output = output
        self.steps = steps


def _jsonable(value):
    """Round-trip through JSON, so checkpoints behave the same in every store."""
    return json.loads(json.dumps(value))


class Flow:
    """A named workflow: steps, branches, and where to keep checkpoints."""

    def __init__(self, name, store=None):
        """Name the flow; store defaults to Supabase tables (see `supie flow migration`)."""
        self.name = name
        self.store = store
        self.order = []
        self._by_name = {}
        self._by_fn = {}

    def _register(self, depends_on, retries, timeout, backoff, branch):
        """Return a decorator that registers a step with these settings."""

        def register(fn):
            """Add the step and return the function unchanged."""
            names = []
            for dep in depends_on:
                step = self._by_fn.get(dep)
                if step is None:
                    raise ValueError(
                        "depends_on must name steps registered earlier in this flow"
                    )
                if step.branch and not branch:
                    raise ValueError(
                        "only branches can depend on a branch: " + step.name
                    )
                names.append(step.name)
            name = fn.__name__
            if name in self._by_name:
                raise ValueError("duplicate step: " + name)
            step = Step(fn, name, names, retries, timeout, backoff, branch)
            self.order.append(step)
            self._by_name[name] = step
            self._by_fn[fn] = step
            return fn

        return register

    def step(self, depends_on=(), retries=0, timeout=None, backoff=1.0):
        """Register a step. It runs once all of depends_on are done.

        retries: extra attempts after a failure, waiting backoff, 2*backoff, ...
        seconds between them. timeout: seconds before an async step counts as failed.
        """
        return self._register(depends_on, retries, timeout, backoff, False)

    def branch(self, depends_on=(), retries=0, timeout=None, backoff=1.0):
        """Register a branch: a step that runs only when another step goes to it."""
        return self._register(depends_on, retries, timeout, backoff, True)

    def _store(self):
        """The checkpoint store, creating the Supabase one on first use."""
        if self.store is None:
            self.store = SupabaseStore()
        return self.store

    def _result(self, done):
        """The run's output: the last step's, or a dict when several steps end it."""
        needed = set()
        for step in self.order:
            if not step.branch:
                needed.update(step.depends_on)
        ends = [s.name for s in self.order if not s.branch and s.name not in needed]
        if len(ends) == 1:
            return done.get(ends[0])
        return {name: done.get(name) for name in ends}

    async def run(self, input=None, run_id=None, request=None, owner=None):
        """Run the flow to the end and return a Run, raising StepFailed if a step fails.

        With the run_id of an earlier run, saved steps are skipped and the saved
        input is used. Pass the request so steps act as the caller in Supabase.
        """
        store = self._store()
        run_id = run_id or new_id()
        saved = await store.load(run_id)
        if saved is None:
            input = _jsonable(input)
            await store.create(run_id, self.name, input, owner)
            done = {}
        else:
            if saved["flow"] != self.name:
                raise ValueError(run_id + " is a run of " + saved["flow"])
            input = saved["input"]
            done = dict(saved["steps"])
            if saved["status"] == "succeeded":
                return Run(run_id, "succeeded", saved["output"], done)
        try:
            pending = [s for s in self.order if not s.branch and s.name not in done]
            while pending:
                ready = [s for s in pending if all(d in done for d in s.depends_on)]
                outputs = await gather(
                    *[self._execute(s, run_id, input, done, request) for s in ready]
                )
                for step, output in zip(ready, outputs):
                    done[step.name] = output
                pending = [s for s in pending if s.name not in done]
        except StepFailed as error:
            await store.finish(run_id, "failed", None, str(error))
            raise
        output = self._result(done)
        await store.finish(run_id, "succeeded", output, None)
        return Run(run_id, "succeeded", output, done)

    async def _execute(self, step, run_id, input, done, request):
        """Run one step with its retries and timeout, follow a goto, and save it."""
        store = self._store()
        args = [done[name] for name in step.depends_on]
        attempt = 0
        while True:
            attempt += 1
            ctx = Context(self, run_id, step.name, attempt, request)
            try:
                result = step.fn(ctx, input, *args)
                if hasattr(result, "send"):
                    if step.timeout:
                        result = await wait_for(result, step.timeout)
                    else:
                        result = await result
                break
            except Exception as error:
                if attempt > step.retries:
                    await store.save_step(
                        run_id,
                        step.name,
                        "failed",
                        None,
                        attempt,
                        format_exception(error),
                        None,
                    )
                    raise StepFailed(step.name, error, attempt)
                await sleep(step.backoff * 2 ** (attempt - 1))
        goto = None
        if isinstance(result, Goto):
            goto = result.step
            missing = [name for name in goto.depends_on if name not in done]
            if missing:
                error = ValueError(
                    goto.name + " needs " + ", ".join(missing) + " first"
                )
                raise StepFailed(step.name, error, attempt)
            result = await self._execute(goto, run_id, input, done, request)
            done[goto.name] = result
        result = _jsonable(result)
        await store.save_step(
            run_id,
            step.name,
            "succeeded",
            result,
            attempt,
            None,
            goto.name if goto else None,
        )
        return result

    async def _background(self, run_id, request):
        """Run a started flow after the response; failures are already saved."""
        try:
            await self.run(None, run_id, request)
        except StepFailed:
            pass

    def mount(self, app, prefix):
        """Add endpoints to start a run, read its status and resume it.

        POST prefix starts a run with the JSON body as input and answers 202 with
        its run_id. GET prefix/<run_id> reads the run. POST prefix/<run_id>/resume
        continues a run whose worker stopped. POST prefix/resume-stale, for the
        service role only, resumes every run idle for over ?idle=<seconds> (300).
        A run started by a signed-in user is visible only to that user.
        """
        flow = self

        @app.post(prefix)
        async def start(req):
            """Start a run in the background."""
            store = flow._store()
            run_id = new_id()
            body = req.json() if req.body else None
            await store.create(run_id, flow.name, _jsonable(body), _subject(req))
            req.background(flow._background(run_id, req))
            return Response(
                {"run_id": run_id, "status": "running"},
                status=202,
                headers={"location": prefix + "/" + run_id},
            )

        @app.post(prefix + "/resume-stale")
        async def resume_stale(req):
            """Resume runs whose worker stopped (service role only)."""
            if (req.user or {}).get("role") != "service_role":
                raise HTTPError(403, "needs the service role")
            idle = int(req.query.get("idle", "300"))
            ids = await flow._store().stale(flow.name, idle)
            for run_id in ids:
                req.background(flow._background(run_id, None))
            return {"resumed": ids}

        @app.get(prefix + "/{run_id}")
        async def status(req, run_id):
            """Read a run and its steps."""
            saved = await _visible(flow, req, run_id)
            return {
                "run_id": run_id,
                "flow": saved["flow"],
                "status": saved["status"],
                "output": saved["output"],
                "error": saved["error"],
                "steps": saved["steps"],
            }

        @app.post(prefix + "/{run_id}/resume")
        async def resume(req, run_id):
            """Continue a run from its last saved step."""
            saved = await _visible(flow, req, run_id)
            if saved["status"] == "succeeded":
                return {"run_id": run_id, "status": "succeeded"}
            req.background(flow._background(run_id, req))
            return Response({"run_id": run_id, "status": "running"}, status=202)

        return flow


def _subject(req):
    """The signed-in user's ID, or None for anonymous and service role callers."""
    claims = req.user or {}
    if claims.get("role") == "authenticated":
        return claims.get("sub")
    return None


async def _visible(flow, req, run_id):
    """Load a run the caller may see, or answer 404."""
    saved = await flow._store().load(run_id)
    if saved is None or saved["flow"] != flow.name:
        raise HTTPError(404, "no such run")
    claims = req.user or {}
    owner = saved.get("owner")
    if owner and claims.get("role") != "service_role" and claims.get("sub") != owner:
        raise HTTPError(404, "no such run")
    return saved


class MemoryStore:
    """Checkpoints in memory: for tests, or runs that needn't survive the worker."""

    def __init__(self):
        """Start empty."""
        self.runs = {}
        self.clock = 0

    def _tick(self, run):
        """Record activity on a run."""
        self.clock += 1
        run["updated"] = self.clock

    async def load(self, run_id):
        """Return the run with its succeeded steps' outputs, or None."""
        run = self.runs.get(run_id)
        if run is None:
            return None
        steps = {}
        for name, step in run["step_rows"].items():
            if step["status"] == "succeeded":
                steps[name] = step["output"]
        saved = dict(run)
        saved["steps"] = steps
        return saved

    async def create(self, run_id, flow, input, owner):
        """Record a new running run."""
        self.runs[run_id] = {
            "flow": flow,
            "status": "running",
            "input": input,
            "owner": owner,
            "output": None,
            "error": None,
            "step_rows": {},
        }
        self._tick(self.runs[run_id])

    async def save_step(self, run_id, step, status, output, attempts, error, goto):
        """Record a finished or failed step."""
        run = self.runs[run_id]
        run["step_rows"][step] = {
            "status": status,
            "output": output,
            "attempts": attempts,
            "error": error,
            "goto": goto,
        }
        self._tick(run)

    async def finish(self, run_id, status, output, error):
        """Record how the run ended."""
        run = self.runs[run_id]
        run["status"] = status
        run["output"] = output
        run["error"] = error
        self._tick(run)

    async def stale(self, flow, idle_seconds):
        """Running runs of the flow. Memory runs have no wall clock, so idle is ignored."""
        return [
            run_id
            for run_id, run in self.runs.items()
            if run["flow"] == flow and run["status"] == "running"
        ]


class SupabaseStore:
    """Checkpoints in the supie_flow_runs and supie_flow_steps tables, as the service role."""

    def __init__(self, client=None):
        """Use this client, or a service role client from the environment."""
        self.client = client

    def _db(self):
        """The client, created on first use so the module imports without a host."""
        if self.client is None:
            from supie.supabase import Supabase

            key = env["SUPABASE_SERVICE_ROLE_KEY"]
            self.client = Supabase(env["SUPABASE_URL"], key, key)
        return self.client

    async def load(self, run_id):
        """Return the run with its succeeded steps' outputs, or None."""
        db = self._db()
        runs = await db.table("supie_flow_runs").select("*").eq("id", run_id).execute()
        if not runs.data:
            return None
        rows = await (
            db.table("supie_flow_steps")
            .select("step, status, output")
            .eq("run_id", run_id)
            .execute()
        )
        saved = runs.data[0]
        saved["steps"] = {
            row["step"]: row["output"]
            for row in rows.data
            if row["status"] == "succeeded"
        }
        return saved

    async def create(self, run_id, flow, input, owner):
        """Insert a new running run."""
        row = {"id": run_id, "flow": flow, "input": input, "owner": owner}
        await self._db().table("supie_flow_runs").insert(row).execute()

    async def save_step(self, run_id, step, status, output, attempts, error, goto):
        """Insert or replace a step's row."""
        row = {
            "run_id": run_id,
            "step": step,
            "status": status,
            "output": output,
            "attempts": attempts,
            "error": error,
            "goto": goto,
        }
        await (
            self._db()
            .table("supie_flow_steps")
            .upsert(row, on_conflict="run_id,step")
            .execute()
        )

    async def finish(self, run_id, status, output, error):
        """Record how the run ended."""
        values = {"status": status, "output": output, "error": error}
        await (
            self._db()
            .table("supie_flow_runs")
            .update(values)
            .eq("id", run_id)
            .execute()
        )

    async def stale(self, flow, idle_seconds):
        """IDs of the flow's running runs with no activity for idle_seconds."""
        result = await self._db().rpc(
            "supie_flow_stale", {"flow_name": flow, "idle_seconds": idle_seconds}
        )
        return result.data or []
