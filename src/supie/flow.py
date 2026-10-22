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

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

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

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from typing import Protocol, TypeVar

    from supie.app import App, Request
    from supie.supabase import Supabase

    StepFn = Callable[..., Any]
    F = TypeVar("F", bound=Callable[..., Any])

    class Store(Protocol):
        """Where a flow keeps runs and their steps' outputs."""

        async def load(self, run_id: str) -> dict[str, Any] | None:
            """The run with its succeeded steps' outputs under "steps", or None."""
            ...

        async def create(
            self, run_id: str, flow: str, input: Any, owner: str | None
        ) -> None:
            """Record a new running run."""
            ...

        async def save_step(
            self,
            run_id: str,
            step: str,
            status: str,
            output: Any,
            attempts: int,
            error: str | None,
            goto: str | None,
        ) -> None:
            """Record a step that succeeded, failed or was skipped."""
            ...

        async def finish(
            self, run_id: str, status: str, output: Any, error: str | None
        ) -> None:
            """Record how the run ended."""
            ...

        async def stale(self, flow: str, idle_seconds: int) -> list[str]:
            """IDs of the flow's running runs idle for idle_seconds."""
            ...


class StepFailed(Exception):
    """A step raised on its last attempt; the run is marked failed."""

    def __init__(self, step: str, error: BaseException, attempts: int) -> None:
        """Store the step's name, the error and how many attempts were made."""
        super().__init__(
            step + " failed after " + str(attempts) + " attempt(s): " + repr(error)
        )
        self.step = step
        self.error = error
        self.attempts = attempts


class Goto:
    """Returned by ctx.goto: hand this step's result to a branch."""

    def __init__(self, step: Step) -> None:
        """Keep the branch to run."""
        self.step = step


class Step:
    """A registered step: the function and how to run it."""

    def __init__(
        self,
        fn: StepFn,
        name: str,
        depends_on: list[str],
        retries: int,
        timeout: float | None,
        backoff: float,
        branch: bool,
        when: dict[str, Any] | None,
        unless: dict[str, Any] | None,
    ) -> None:
        """Store the step's function, settings and the names it depends on."""
        self.fn = fn
        self.name = name
        self.depends_on = depends_on
        self.retries = retries
        self.timeout = timeout
        self.backoff = backoff
        self.branch = branch
        self.when = when
        self.unless = unless


SKIPPED = object()  # a step whose when/unless condition wasn't met


def contains(value: Any, pattern: Any) -> bool:
    """JSON containment, as Postgres's @> on jsonb: does value include pattern?"""
    if isinstance(pattern, dict):
        if not isinstance(value, dict):
            return False
        for key, sub in pattern.items():
            if key not in value or not contains(value[key], sub):
                return False
        return True
    if isinstance(pattern, list):
        if not isinstance(value, list):
            return False
        for sub in pattern:
            if not any(contains(item, sub) for item in value):
                return False
        return True
    if isinstance(pattern, bool) or isinstance(value, bool):
        return (
            isinstance(pattern, bool) and isinstance(value, bool) and pattern == value
        )
    return value == pattern


class Context:
    """What a step can use: the run, the attempt, Supabase and goto."""

    def __init__(
        self,
        flow: Flow,
        run_id: str,
        step: str,
        attempt: int,
        request: Request | None,
    ) -> None:
        """Store the run and step this context belongs to."""
        self.flow = flow
        self.run_id = run_id
        self.step = step
        self.attempt = attempt
        self.request = request

    @property
    def supabase(self) -> Supabase:
        """A client acting as the caller when the run came from a request, else the service role."""
        if self.request is not None:
            return self.request.supabase
        return self.supabase_admin

    @property
    def supabase_admin(self) -> Supabase:
        """A client with the service role key."""
        from supie.supabase import Supabase

        key = env["SUPABASE_SERVICE_ROLE_KEY"]
        return Supabase(env["SUPABASE_URL"], key, key)

    async def invoke(
        self, name: str, body: Any = None, headers: dict[str, str] | None = None
    ) -> Any:
        """Call another Edge Function and return its parsed response."""
        return await self.supabase.functions.invoke(name, body, headers)

    def goto(self, step: StepFn) -> Goto:
        """Return this from a step to run a branch; the branch's output becomes the step's."""
        target = self.flow._by_fn.get(step)
        if target is None or not target.branch:
            raise ValueError("goto needs a step registered with @flow.branch()")
        return Goto(target)


class Run:
    """A finished run: its ID, status, output and every step's output."""

    def __init__(
        self, run_id: str, status: str, output: Any, steps: dict[str, Any]
    ) -> None:
        """Store the outcome of a run."""
        self.run_id = run_id
        self.status = status
        self.output = output
        self.steps = steps


def _jsonable(value: Any) -> Any:
    """Round-trip through JSON, so checkpoints behave the same in every store."""
    return json.loads(json.dumps(value))


class Flow:
    """A named workflow: steps, branches, and where to keep checkpoints."""

    def __init__(self, name: str, store: Store | None = None) -> None:
        """Name the flow; store defaults to Supabase tables (see `supie flow migration`)."""
        self.name = name
        self.store = store
        self.order: list[Step] = []
        self._by_name: dict[str, Step] = {}
        self._by_fn: dict[Any, Step] = {}
        self._compiled = False  # durable runs: compiled in pgflow by this worker
        self._worker_id: str | None = None

    def _register(
        self,
        depends_on: Sequence[StepFn],
        retries: int,
        timeout: float | None,
        backoff: float,
        branch: bool,
        when: dict[str, Any] | None,
        unless: dict[str, Any] | None,
    ) -> Callable[[F], F]:
        """Return a decorator that registers a step with these settings."""

        def register(fn: F) -> F:
            """Add the step and return the function unchanged."""
            names: list[str] = []
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
            name: str = getattr(fn, "__name__")  # every function has one
            if name in self._by_name:
                raise ValueError("duplicate step: " + name)
            for pattern in (when, unless):
                if pattern is not None and not isinstance(pattern, dict):
                    raise ValueError("when and unless take a dict pattern")
            step = Step(
                fn, name, names, retries, timeout, backoff, branch, when, unless
            )
            self.order.append(step)
            self._by_name[name] = step
            self._by_fn[fn] = step
            return fn

        return register

    def step(
        self,
        depends_on: Sequence[StepFn] = (),
        retries: int = 0,
        timeout: float | None = None,
        backoff: float = 1.0,
        when: dict[str, Any] | None = None,
        unless: dict[str, Any] | None = None,
    ) -> Callable[[F], F]:
        """Register a step. It runs once all of depends_on are done.

        retries: extra attempts after a failure, waiting backoff, 2*backoff, ...
        seconds between them. timeout: seconds before an async step counts as failed.
        when / unless: a JSON pattern the step's input must (or must not) contain,
        else the step is skipped. The input is {dependency: output} for the
        dependencies that ran, or the flow's input for a step without any, so
        when={"decide": {"route": "review"}} runs a step only on that route.
        Dependents of a skipped step still run and get None for it. These are
        pgflow's conditions, so they work in durable runs too.
        """
        return self._register(
            depends_on, retries, timeout, backoff, False, when, unless
        )

    def branch(
        self,
        depends_on: Sequence[StepFn] = (),
        retries: int = 0,
        timeout: float | None = None,
        backoff: float = 1.0,
    ) -> Callable[[F], F]:
        """Register a branch: a step that runs only when another step goes to it.

        Inline runs only; durable runs use when= instead.
        """
        return self._register(depends_on, retries, timeout, backoff, True, None, None)

    def _condition_input(self, step: Step, input: Any, done: dict[str, Any]) -> Any:
        """What when/unless match against: completed dependencies' outputs, or the input."""
        if not step.depends_on:
            return input
        return {
            name: done[name] for name in step.depends_on if done[name] is not SKIPPED
        }

    def _should_skip(self, step: Step, input: Any, done: dict[str, Any]) -> bool:
        """Whether the step's when/unless condition rules it out."""
        if step.when is None and step.unless is None:
            return False
        value = self._condition_input(step, input, done)
        if step.when is not None and not contains(value, step.when):
            return True
        return step.unless is not None and contains(value, step.unless)

    def _store(self) -> Store:
        """The checkpoint store, creating the Supabase one on first use."""
        if self.store is None:
            self.store = SupabaseStore()
        return self.store

    def _result(self, done: dict[str, Any]) -> Any:
        """The run's output: the last step's, or a dict when several steps end it."""
        needed = set()
        for step in self.order:
            if not step.branch:
                needed.update(step.depends_on)
        ends = [s.name for s in self.order if not s.branch and s.name not in needed]
        if len(ends) == 1:
            output = done.get(ends[0])
            return None if output is SKIPPED else output
        return {
            name: done[name]
            for name in ends
            if name in done and done[name] is not SKIPPED
        }

    async def run(
        self,
        input: Any = None,
        run_id: str | None = None,
        request: Request | None = None,
        owner: str | None = None,
    ) -> Run:
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
        ran = {name: out for name, out in done.items() if out is not SKIPPED}
        return Run(run_id, "succeeded", output, ran)

    async def _execute(
        self,
        step: Step,
        run_id: str,
        input: Any,
        done: dict[str, Any],
        request: Request | None,
    ) -> Any:
        """Run one step with its retries and timeout, follow a goto, and save it."""
        store = self._store()
        if self._should_skip(step, input, done):
            await store.save_step(run_id, step.name, "skipped", None, 0, None, None)
            return SKIPPED
        args = [
            None if done[name] is SKIPPED else done[name] for name in step.depends_on
        ]
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

    async def _background(self, run_id: str, request: Request | None) -> None:
        """Run a started flow after the response; failures are already saved."""
        try:
            await self.run(None, run_id, request)
        except StepFailed:
            pass

    # Durable runs on pgflow: Postgres schedules each step and keeps every
    # output; workers claim tasks through the supie_pgflow_* functions
    # (`supie flow migration --pgflow`). A run outlives any one worker.

    def shape(self) -> dict[str, Any]:
        """The flow as pgflow's shape, for pgflow.ensure_flow_compiled."""
        _check_slug(self.name, "flow")
        steps = []
        for step in self.order:
            if step.branch:
                raise ValueError(
                    step.name
                    + " is a branch (goto), which durable runs don't support; "
                    "use @flow.step(depends_on=..., when=...) instead"
                )
            _check_slug(step.name, "step")
            options = {"maxAttempts": step.retries + 1}
            options["baseDelay"] = max(1, int(step.backoff))
            if step.timeout:
                options["timeout"] = max(1, int(step.timeout))
            steps.append(
                {
                    "slug": step.name,
                    "stepType": "single",
                    "dependencies": sorted(step.depends_on),
                    "whenUnmet": "skip",
                    "whenExhausted": "fail",
                    "requiredInputPattern": _pattern(step.when),
                    "forbiddenInputPattern": _pattern(step.unless),
                    "options": options,
                }
            )
        return {"steps": steps}

    def _pgflow(self) -> Supabase:
        """A service role client for the supie_pgflow_* functions."""
        from supie.supabase import Supabase

        key = env["SUPABASE_SERVICE_ROLE_KEY"]
        return Supabase(env["SUPABASE_URL"], key, key)

    async def _ensure_compiled(self) -> None:
        """Compile the flow in pgflow once per worker, or check it matches."""
        if self._compiled:
            return
        result = await self._pgflow().rpc(
            "supie_pgflow_compile", {"flow_slug": self.name, "shape": self.shape()}
        )
        if (result.data or {}).get("status") == "mismatch":
            raise RuntimeError(
                self.name
                + " in pgflow differs from this code: "
                + "; ".join(result.data.get("differences") or [])
            )
        self._compiled = True

    async def start(self, input: Any = None, request: Request | None = None) -> str:
        """Start a durable run on pgflow and return its ID; workers run its steps."""
        await self._ensure_compiled()
        owner = _subject(request) if request is not None else None
        result = await self._pgflow().rpc(
            "supie_pgflow_start",
            {"flow_slug": self.name, "input": _jsonable(input), "owner": owner},
        )
        return result.data

    async def status(self, run_id: str) -> dict[str, Any] | None:
        """A durable run: its status, output and each step's state, or None."""
        result = await self._pgflow().rpc("supie_pgflow_run", {"run_id": run_id})
        data = result.data
        if not data or not data.get("run"):
            return None
        run = data["run"]
        output = run.get("output")
        ends = self._ends()
        if len(ends) == 1 and isinstance(output, dict):
            output = output.get(ends[0])
        steps = {}
        for state in data.get("steps") or []:
            steps[state["step_slug"]] = {
                "status": state["status"],
                "output": state.get("output"),
                "error": state.get("error_message"),
            }
        return {
            "run_id": run_id,
            "flow": self.name,
            "status": run["status"],
            "output": output,
            "steps": steps,
            "owner": data.get("owner"),
        }

    def _ends(self) -> list[str]:
        """The steps nothing depends on: their outputs make up the result."""
        needed = set()
        for step in self.order:
            needed.update(step.depends_on)
        return [s.name for s in self.order if not s.branch and s.name not in needed]

    async def work(
        self,
        request: Request | None = None,
        seconds: float = 60,
        idle_polls: int = 2,
        batch: int = 10,
    ) -> int:
        """Claim and run tasks until the queue is idle or seconds have passed.

        Returns how many tasks ran. Each task's CPU counts against this
        invocation's budget, so a worker stops early rather than run forever;
        another invocation (or pg_cron calling the work endpoint) continues.
        """
        await self._ensure_compiled()
        db = self._pgflow()
        if self._worker_id is None:
            hex_id = new_id()
            self._worker_id = "-".join(
                [hex_id[:8], hex_id[8:12], hex_id[12:16], hex_id[16:20], hex_id[20:32]]
            )
            await db.rpc(
                "supie_pgflow_register",
                {
                    "worker_id": self._worker_id,
                    "flow_slug": self.name,
                    "function_name": env.get("SB_FUNCTION_NAME", "supie"),
                },
            )
        ran = 0
        idle = 0
        deadline = _now() + seconds
        while idle < idle_polls and _now() < deadline:
            worker = {"flow_slug": self.name, "worker_id": self._worker_id}
            read = await db.rpc("supie_pgflow_read", dict(worker, qty=batch))
            if not read.data:
                idle += 1
                continue
            claimed = await db.rpc(
                "supie_pgflow_claim", dict(worker, msg_ids=read.data)
            )
            tasks = claimed.data or []
            idle = 0
            await gather(*[self._run_task(db, task, request) for task in tasks])
            ran += len(tasks)
        return ran

    async def _run_task(
        self, db: Supabase, task: dict[str, Any], request: Request | None
    ) -> None:
        """Run one claimed task and report its output or error to pgflow."""
        step = self._by_name[task["step_slug"]]
        outputs = task.get("input") or {}
        args = [outputs.get(name) for name in step.depends_on]
        ctx = Context(
            self, task["run_id"], step.name, task.get("attempt") or 1, request
        )
        key = {
            "run_id": task["run_id"],
            "step_slug": step.name,
            "task_index": task["task_index"],
        }
        try:
            result = step.fn(ctx, task.get("flow_input"), *args)
            if hasattr(result, "send"):
                if step.timeout:
                    result = await wait_for(result, step.timeout)
                else:
                    result = await result
            if isinstance(result, Goto):
                raise ValueError("goto isn't supported in durable runs; use when=")
            payload = dict(key)
            payload["output"] = _jsonable(result)
            await db.rpc("supie_pgflow_complete", payload)
        except Exception as error:
            payload = dict(key)
            payload["error_message"] = format_exception(error)[-2000:]
            await db.rpc("supie_pgflow_fail", payload)

    def mount(self, app: App, prefix: str, durable: bool = False) -> Flow:
        """Add endpoints to start a run, read its status and resume it.

        With durable=True the run happens on pgflow instead (see _mount_durable).

        POST prefix starts a run with the JSON body as input and answers 202 with
        its run_id. GET prefix/<run_id> reads the run. POST prefix/<run_id>/resume
        continues a run whose worker stopped. POST prefix/resume-stale, for the
        service role only, resumes every run idle for over ?idle=<seconds> (300).
        A run started by a signed-in user is visible only to that user.
        """
        if durable:
            return self._mount_durable(app, prefix)
        flow = self

        @app.post(prefix)
        async def start(req: Request) -> Response:
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
        async def resume_stale(req: Request) -> dict[str, Any]:
            """Resume runs whose worker stopped (service role only)."""
            if (req.user or {}).get("role") != "service_role":
                raise HTTPError(403, "needs the service role")
            idle = int(req.query.get("idle", "300"))
            ids = await flow._store().stale(flow.name, idle)
            for run_id in ids:
                req.background(flow._background(run_id, None))
            return {"resumed": ids}

        @app.get(prefix + "/{run_id}")
        async def status(req: Request, run_id: str) -> dict[str, Any]:
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
        async def resume(req: Request, run_id: str) -> Any:
            """Continue a run from its last saved step."""
            saved = await _visible(flow, req, run_id)
            if saved["status"] == "succeeded":
                return {"run_id": run_id, "status": "succeeded"}
            req.background(flow._background(run_id, req))
            return Response({"run_id": run_id, "status": "running"}, status=202)

        return flow

    def _mount_durable(self, app: App, prefix: str) -> Flow:
        """Endpoints for durable runs on pgflow.

        POST prefix starts a run with the JSON body as input, answers 202 with
        its run_id, and works on it after the response. GET prefix/<run_id>
        reads it. POST prefix/work, for the service role, runs waiting tasks:
        call it from pg_cron so runs continue after a worker stops.
        """
        flow = self

        @app.post(prefix)
        async def start_durable(req: Request) -> Response:
            """Start a run on pgflow and work on it in the background."""
            body = req.json() if req.body else None
            run_id = await flow.start(body, req)
            req.background(flow.work(req))
            return Response(
                {"run_id": run_id, "status": "started"},
                status=202,
                headers={"location": prefix + "/" + run_id},
            )

        @app.post(prefix + "/work")
        async def work_durable(req: Request) -> dict[str, int]:
            """Run waiting tasks (service role only)."""
            if (req.user or {}).get("role") != "service_role":
                raise HTTPError(403, "needs the service role")
            seconds = int(req.query.get("seconds", "60"))
            return {"ran": await flow.work(req, seconds=seconds)}

        @app.get(prefix + "/{run_id}")
        async def status_durable(req: Request, run_id: str) -> dict[str, Any]:
            """Read a run and its steps."""
            state = await flow.status(run_id)
            claims = req.user or {}
            owner = state and state.get("owner")
            hidden = owner and claims.get("role") != "service_role"
            if state is None or (hidden and claims.get("sub") != owner):
                raise HTTPError(404, "no such run")
            state.pop("owner")
            return state

        return flow


def _check_slug(name: str, kind: str) -> None:
    """pgflow's rule for flow and step names: letters, digits, single underscores."""

    def alnum(char: str) -> bool:
        """A letter or digit (MicroPython's str has no isalnum)."""
        return char.isalpha() or char.isdigit()

    ok = name and name != "run" and name[0].isalpha() and alnum(name[-1])
    ok = ok and "__" not in name and len(name) <= 128
    ok = ok and all(alnum(c) or c == "_" for c in name)
    if not ok:
        raise ValueError(
            kind + " name " + repr(name) + " isn't valid in pgflow: use letters, "
            "digits and single underscores, starting with a letter"
        )


def _pattern(value: dict[str, Any] | None) -> dict[str, Any]:
    """A when/unless pattern in pgflow's shape format."""
    if value is None:
        return {"defined": False}
    return {"defined": True, "value": value}


def _now() -> float:
    """Seconds since an arbitrary point, for deadlines."""
    import time

    if hasattr(time, "ticks_ms"):
        return time.ticks_ms() / 1000
    return time.time()


def _subject(req: Request) -> str | None:
    """The signed-in user's ID, or None for anonymous and service role callers."""
    claims = req.user or {}
    if claims.get("role") == "authenticated":
        return claims.get("sub")
    return None


async def _visible(flow: Flow, req: Request, run_id: str) -> dict[str, Any]:
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

    def __init__(self) -> None:
        """Start empty."""
        self.runs: dict[str, dict[str, Any]] = {}
        self.clock = 0

    def _tick(self, run: dict[str, Any]) -> None:
        """Record activity on a run."""
        self.clock += 1
        run["updated"] = self.clock

    async def load(self, run_id: str) -> dict[str, Any] | None:
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

    async def create(
        self, run_id: str, flow: str, input: Any, owner: str | None
    ) -> None:
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

    async def save_step(
        self,
        run_id: str,
        step: str,
        status: str,
        output: Any,
        attempts: int,
        error: str | None,
        goto: str | None,
    ) -> None:
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

    async def finish(
        self, run_id: str, status: str, output: Any, error: str | None
    ) -> None:
        """Record how the run ended."""
        run = self.runs[run_id]
        run["status"] = status
        run["output"] = output
        run["error"] = error
        self._tick(run)

    async def stale(self, flow: str, idle_seconds: int) -> list[str]:
        """Running runs of the flow. Memory runs have no wall clock, so idle is ignored."""
        return [
            run_id
            for run_id, run in self.runs.items()
            if run["flow"] == flow and run["status"] == "running"
        ]


class SupabaseStore:
    """Checkpoints in the supie_flow_runs and supie_flow_steps tables, as the service role."""

    def __init__(self, client: Supabase | None = None) -> None:
        """Use this client, or a service role client from the environment."""
        self.client = client

    def _db(self) -> Supabase:
        """The client, created on first use so the module imports without a host."""
        if self.client is None:
            from supie.supabase import Supabase

            key = env["SUPABASE_SERVICE_ROLE_KEY"]
            self.client = Supabase(env["SUPABASE_URL"], key, key)
        return self.client

    async def load(self, run_id: str) -> dict[str, Any] | None:
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

    async def create(
        self, run_id: str, flow: str, input: Any, owner: str | None
    ) -> None:
        """Insert a new running run."""
        row = {"id": run_id, "flow": flow, "input": input, "owner": owner}
        await self._db().table("supie_flow_runs").insert(row).execute()

    async def save_step(
        self,
        run_id: str,
        step: str,
        status: str,
        output: Any,
        attempts: int,
        error: str | None,
        goto: str | None,
    ) -> None:
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

    async def finish(
        self, run_id: str, status: str, output: Any, error: str | None
    ) -> None:
        """Record how the run ended."""
        values = {"status": status, "output": output, "error": error}
        await (
            self._db()
            .table("supie_flow_runs")
            .update(values)
            .eq("id", run_id)
            .execute()
        )

    async def stale(self, flow: str, idle_seconds: int) -> list[str]:
        """IDs of the flow's running runs with no activity for idle_seconds."""
        result = await self._db().rpc(
            "supie_flow_stale", {"flow_name": flow, "idle_seconds": idle_seconds}
        )
        return result.data or []
