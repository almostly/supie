"""Test handlers under plain CPython, without Pyodide, MicroPython or Supabase.

from supie.testing import TestClient
from main import app

def test_score():
    client = TestClient(app)
    assert client.post("/", json={"age": 40}).status == 200
"""

from __future__ import annotations

import asyncio
import base64
import json as jsonlib
import uuid
from collections.abc import Callable
from typing import Any

from supie import app as runtime


class FakeResponse:
    """What a fake fetch returns: a status, headers and a body."""

    def __init__(
        self,
        body: Any = None,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        """Store the response; a dict or list body is sent as JSON."""
        self.status = status
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        if isinstance(body, (dict, list)):
            self.body: str | bytes = jsonlib.dumps(body)
            self.headers.setdefault("content-type", "application/json")
        else:
            self.body = body if body is not None else ""


class Call:
    """One request a handler made through fetch."""

    def __init__(
        self, method: str, url: str, headers: dict[str, str], body: str | bytes | None
    ) -> None:
        """Store the method, URL, headers and body of the request."""
        self.method = method
        self.url = url
        self.headers = headers
        self.body = body

    def json(self) -> Any:
        """Parse the request body as JSON."""
        return jsonlib.loads(self.body or "null")


Handler = Callable[[Call], FakeResponse]


class FakeHost:
    """Stands in for the shim: environment variables and a fetch you control."""

    def __init__(self, env: dict[str, str] | None = None, fetch: Handler | None = None):
        """Use these variables, and answer fetches with the handler (404 without one)."""
        self.vars = dict(env or {})
        self.handler = fetch
        self.calls: list[Call] = []
        self.sleep_scale = 1.0
        self.sessions: list[Any] = []

    def env(self, name: str) -> str | None:
        """Return an environment variable, or None."""
        return self.vars.get(name)

    async def gather(self, *tasks: Any) -> None:
        """Run tasks concurrently, as the shim's Promise.all does."""
        await asyncio.gather(*tasks)

    async def race(self, *tasks: Any) -> None:
        """Wait for the first task to finish and cancel the rest."""
        pending = [asyncio.ensure_future(task) for task in tasks]
        _, rest = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        for task in rest:
            task.cancel()

    async def sleep(self, ms: int) -> None:
        """Sleep, scaled by sleep_scale so retry delays don't slow tests down."""
        await asyncio.sleep(ms / 1000 * self.sleep_scale)

    def uuid(self) -> str:
        """A random hex ID."""
        return uuid.uuid4().hex

    async def onnx_load(self, url: str, headers_json: str) -> str:
        """Fetch a model through the fake fetch and load it with onnxruntime."""
        try:
            import onnxruntime
        except ImportError as error:
            raise RuntimeError(
                "testing ONNX models under CPython needs onnxruntime: "
                "pip install onnxruntime"
            ) from error
        call = Call("GET", url, jsonlib.loads(headers_json), None)
        self.calls.append(call)
        response = self.handler(call) if self.handler else FakeResponse("", 404)
        if response.status >= 400:
            raise RuntimeError(f"loading {url}: HTTP {response.status}")
        data = (
            response.body
            if isinstance(response.body, bytes)
            else response.body.encode()
        )
        session = onnxruntime.InferenceSession(data)
        self.sessions.append(session)
        return jsonlib.dumps(
            {
                "handle": len(self.sessions) - 1,
                "inputs": [i.name for i in session.get_inputs()],
                "outputs": [o.name for o in session.get_outputs()],
            }
        )

    async def onnx_run(self, handle: int, inputs_json: str) -> str:
        """Run a loaded model, returning nested lists as the shim does."""
        import numpy

        session = self.sessions[handle]
        feeds = {
            name: numpy.asarray(spec["data"], dtype=spec["type"])
            for name, spec in jsonlib.loads(inputs_json).items()
        }
        names = [o.name for o in session.get_outputs()]
        outputs = session.run(names, feeds)
        return jsonlib.dumps(
            {name: numpy.asarray(value).tolist() for name, value in zip(names, outputs)}
        )

    async def fetch(
        self,
        url: str,
        method: str,
        headers_json: str,
        body: str | None,
        body_base64: bool = False,
        want_base64: bool = False,
    ) -> str:
        """Record the call and answer it with the handler, as the shim's fetch would."""
        raw: str | bytes | None = body
        if body is not None and body_base64:
            raw = base64.b64decode(body)
        call = Call(method, url, jsonlib.loads(headers_json), raw)
        self.calls.append(call)
        response = self.handler(call) if self.handler else FakeResponse("", 404)
        payload = response.body
        if want_base64:
            data = payload.encode() if isinstance(payload, str) else payload
            payload = base64.b64encode(data).decode()
        elif isinstance(payload, bytes):
            payload = payload.decode()
        return jsonlib.dumps(
            {"status": response.status, "headers": response.headers, "body": payload}
        )


DEFAULT_ENV = {
    "SUPABASE_URL": "https://test.supabase.co",
    "SUPABASE_ANON_KEY": "anon-key",
    "SUPABASE_SERVICE_ROLE_KEY": "service-role-key",
}


class TestResponse:
    """A handler's response, decoded."""

    def __init__(self, wire: dict[str, Any]) -> None:
        """Decode the wire format the shim would turn into an HTTP response."""
        self.status: int = wire["status"]
        self.headers: dict[str, str] = wire["headers"]
        body = wire["body"]
        self.content = base64.b64decode(body) if wire["base64"] else body.encode()
        self.log: str | None = wire["log"]

    @property
    def text(self) -> str:
        """The body as text."""
        return self.content.decode()

    def json(self) -> Any:
        """The body parsed as JSON."""
        return jsonlib.loads(self.content)


class TestClient:
    """Send requests to an App as the function would, and run background tasks."""

    __test__ = False  # not a pytest test class

    def __init__(
        self,
        app: runtime.App,
        env: dict[str, str] | None = None,
        fetch: Handler | None = None,
        raise_errors: bool = True,
    ) -> None:
        """Use the app, environment variables and fake fetch for every request.

        With raise_errors (the default), an unexpected error in a handler fails the
        test with its traceback instead of returning a 500.
        """
        self.app = app
        self.host = FakeHost({**DEFAULT_ENV, **(env or {})}, fetch)
        self.raise_errors = raise_errors
        self.sleep_scale = 1.0  # shrink retry delays: 0 skips them

    @property
    def calls(self) -> list[Call]:
        """The requests handlers made through fetch, in order."""
        return self.host.calls

    def request(
        self,
        method: str,
        path: str,
        json: Any = None,
        body: str | None = None,
        headers: dict[str, str] | None = None,
        query: dict[str, str] | None = None,
    ) -> TestResponse:
        """Send one request and return its response."""
        path, _, raw_query = path.partition("?")
        params = dict(p.split("=", 1) for p in raw_query.split("&") if "=" in p)
        params.update(query or {})
        sent = dict(headers or {})
        if json is not None:
            body = jsonlib.dumps(json)
            sent.setdefault("content-type", "application/json")
        raw = jsonlib.dumps(
            {
                "method": method,
                "path": path,
                "query": params,
                "headers": sent,
                "body": body or "",
            }
        )
        return asyncio.run(self._send(raw))

    async def _send(self, raw: str) -> TestResponse:
        """Dispatch with the fake host bound, then run any background tasks."""
        runtime.set_host(self.host)
        self.host.sleep_scale = self.sleep_scale
        try:
            wire = jsonlib.loads(await runtime.dispatch(self.app, raw))
            if wire["log"] and self.raise_errors:
                raise AssertionError("handler failed:\n" + wire["log"])
            if wire["bg"] is not None:
                log = await runtime.run_background(wire["bg"])
                if log and self.raise_errors:
                    raise AssertionError("background task failed:\n" + log)
            return TestResponse(wire)
        finally:
            runtime.set_host(None)

    def get(self, path: str, **kwargs: Any) -> TestResponse:
        """Send a GET request."""
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> TestResponse:
        """Send a POST request."""
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> TestResponse:
        """Send a PUT request."""
        return self.request("PUT", path, **kwargs)

    def patch(self, path: str, **kwargs: Any) -> TestResponse:
        """Send a PATCH request."""
        return self.request("PATCH", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> TestResponse:
        """Send a DELETE request."""
        return self.request("DELETE", path, **kwargs)


def run(
    awaitable: Any,
    env: dict[str, str] | None = None,
    fetch: Handler | None = None,
    sleep_scale: float = 1.0,
) -> Any:
    """Await something that needs the host, such as flow.run(...), outside a request.

    sleep_scale shrinks sleeps (retry delays); 0 skips them.
    """
    host = FakeHost({**DEFAULT_ENV, **(env or {})}, fetch)
    host.sleep_scale = sleep_scale

    async def bound() -> Any:
        """Await with the fake host bound."""
        runtime.set_host(host)
        try:
            return await awaitable
        finally:
            runtime.set_host(None)

    return asyncio.run(bound())
