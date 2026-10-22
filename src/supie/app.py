"""Handlers, routing, requests and responses: the part of supie that runs in the function.

This module runs under CPython (in tests), MicroPython and Pyodide, so it keeps to
what all three share: no typing, dataclasses, re or inspect. The shim and Python
exchange requests and responses as JSON strings.
"""

import json

REASONS = {
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    409: "Conflict",
    422: "Unprocessable Content",
    429: "Too Many Requests",
    500: "Internal Server Error",
    502: "Bad Gateway",
    503: "Service Unavailable",
}

_host = None  # the shim's JS module in the function, a fake in supie.testing
_pending = {}  # background tasks per request, until the shim runs them
_next_id = [0]


def set_host(host):
    """Bind the host: fetch and environment access, provided by the shim or a fake."""
    global _host
    _host = host


def get_host():
    """Return the bound host, or fail with a hint when there is none."""
    if _host is None:
        raise RuntimeError("no host bound: run in the function, or use supie.testing")
    return _host


class HTTPError(Exception):
    """An error that becomes a response with this status and a JSON error body."""

    def __init__(self, status, detail=None, headers=None):
        """Store the status, the detail shown to the caller and extra headers."""
        if detail is None:
            detail = REASONS.get(status, "Error")
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.headers = headers or {}


class Env:
    """Read-only access to the function's environment variables and secrets."""

    def get(self, name, default=None):
        """Return the variable, or default when it isn't set."""
        value = get_host().env(name)
        return default if value is None else value

    def __getitem__(self, name):
        """Return the variable, raising KeyError when it isn't set."""
        value = get_host().env(name)
        if value is None:
            raise KeyError(name)
        return value

    def __contains__(self, name):
        """Tell whether the variable is set."""
        return get_host().env(name) is not None


env = Env()


class State:
    """A plain namespace for whatever on_boot sets up, shared across requests."""


def _b64url_decode(segment):
    """Decode a base64url segment without padding, as JWTs use."""
    import binascii

    segment = segment.replace("-", "+").replace("_", "/")
    return binascii.a2b_base64((segment + "=" * (-len(segment) % 4)).encode())


class Request:
    """An incoming request, with the body already read."""

    def __init__(self, method, path, query=None, headers=None, body="", params=None):
        """Store the request parts; header names are lower case."""
        self.method = method.upper()
        self.path = path
        self.query = query or {}
        self.headers = {}
        for name, value in (headers or {}).items():
            self.headers[name.lower()] = value
        self.body = body or ""
        self.params = params or {}
        self.app = None
        self._background = []

    def text(self):
        """Return the body as a string."""
        return self.body

    def json(self):
        """Parse the body as JSON, answering 400 when it is missing or invalid."""
        if not self.body:
            raise HTTPError(400, "expected a JSON body")
        try:
            return json.loads(self.body)
        except ValueError:
            raise HTTPError(400, "invalid JSON body")

    @property
    def state(self):
        """The app's shared state, as set up by on_boot."""
        return self.app.state

    @property
    def token(self):
        """The caller's bearer token, or None."""
        auth = self.headers.get("authorization", "")
        if auth[:7].lower() == "bearer ":
            return auth[7:].strip()
        return None

    @property
    def user(self):
        """The caller's JWT claims, or None. The Supabase gateway verifies the JWT."""
        token = self.token
        if not token or token.count(".") != 2:
            return None
        try:
            return json.loads(_b64url_decode(token.split(".")[1]))
        except Exception:
            return None

    @property
    def supabase(self):
        """A Supabase client acting as the caller, so row level security applies."""
        from supie.supabase import Supabase

        key = env["SUPABASE_ANON_KEY"]
        return Supabase(env["SUPABASE_URL"], key, self.token or key)

    @property
    def supabase_admin(self):
        """A Supabase client with the service role key, which bypasses row level security."""
        from supie.supabase import Supabase

        key = env["SUPABASE_SERVICE_ROLE_KEY"]
        return Supabase(env["SUPABASE_URL"], key, key)

    def background(self, task):
        """Run a coroutine after the response is sent."""
        self._background.append(task)


class Response:
    """A response with full control over the body, status and headers.

    A dict or list body becomes JSON, a str becomes text, bytes stay binary and
    None sends no body.
    """

    def __init__(self, body=None, status=200, headers=None, media_type=None):
        """Store the body, status, headers and an optional content type."""
        self.body = body
        self.status = status
        self.headers = {}
        for name, value in (headers or {}).items():
            self.headers[name.lower()] = value
        self.media_type = media_type

    def wire(self):
        """Encode the response for the shim: status, headers and a string body."""
        body = self.body
        headers = dict(self.headers)
        encoded = False
        if body is None:
            text = ""
        elif isinstance(body, (bytes, bytearray)):
            import binascii

            text = binascii.b2a_base64(bytes(body)).decode().strip()
            encoded = True
            headers.setdefault(
                "content-type", self.media_type or "application/octet-stream"
            )
        elif isinstance(body, str):
            text = body
            headers.setdefault(
                "content-type", self.media_type or "text/plain; charset=utf-8"
            )
        else:
            text = json.dumps(body)
            headers.setdefault("content-type", self.media_type or "application/json")
        return {
            "status": self.status,
            "headers": headers,
            "body": text,
            "base64": encoded,
        }


def _to_response(result):
    """Turn whatever a handler returned into a Response."""
    if isinstance(result, Response):
        return result
    if result is None:
        return Response(None, status=204)
    return Response(result)


def _error_response(error):
    """Turn an HTTPError into a JSON error response."""
    return Response({"error": error.detail}, status=error.status, headers=error.headers)


async def _maybe_await(value):
    """Await a coroutine, or pass any other value through."""
    if hasattr(value, "send"):
        return await value
    return value


class Timeout(Exception):
    """An awaitable passed to wait_for didn't finish in time."""


async def sleep(seconds):
    """Wait without blocking other requests or tasks."""
    await get_host().sleep(int(seconds * 1000))


async def gather(*tasks):
    """Run awaitables concurrently and return their results in order.

    Every task runs to the end; if any raised, the first error is raised after.
    """
    results = [None] * len(tasks)
    errors = [None] * len(tasks)

    async def one(i, task):
        """Run one task, keeping its result or error for after the others."""
        try:
            results[i] = await task
        except Exception as error:
            errors[i] = error

    await get_host().gather(*[one(i, task) for i, task in enumerate(tasks)])
    for error in errors:
        if error is not None:
            raise error
    return results


async def wait_for(task, seconds):
    """Await task, raising Timeout if it takes longer than seconds.

    In the function the task can't be cancelled: it keeps running, and its
    result is dropped.
    """
    state = {"done": False, "value": None, "error": None}

    async def body():
        """Run the task and record how it ended."""
        try:
            state["value"] = await task
        except Exception as error:
            state["error"] = error
        state["done"] = True

    await get_host().race(body(), get_host().sleep(int(seconds * 1000)))
    if not state["done"]:
        raise Timeout("timed out after " + str(seconds) + " s")
    if state["error"] is not None:
        raise state["error"]
    return state["value"]


def compare_digest(a, b):
    """Compare two str or bytes values in constant time, for signatures and tokens.

    hmac.compare_digest where it exists; MicroPython lacks it.
    """
    if isinstance(a, str):
        a = a.encode()
    if isinstance(b, str):
        b = b.encode()
    try:
        import hmac

        return hmac.compare_digest(a, b)
    except (ImportError, AttributeError):
        pass
    result = len(a) ^ len(b)
    for x, y in zip(a, b + bytes(max(0, len(a) - len(b)))):
        result |= x ^ y
    return result == 0


def new_id():
    """A random 128-bit hex ID from the platform's CSPRNG."""
    return str(get_host().uuid())


def format_exception(error):
    """Format a traceback under CPython or MicroPython."""
    try:
        import traceback

        return "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        )
    except Exception:
        import io
        import sys

        buffer = io.StringIO()
        sys.print_exception(error, buffer)
        return buffer.getvalue()


_CONVERTERS = {"str": str, "int": int, "float": float, "path": str}


class Route:
    """A method and a path pattern such as /loans/{loan_id:int}, bound to a handler."""

    def __init__(self, methods, pattern, handler):
        """Parse the pattern into literal and parameter segments."""
        if not pattern.startswith("/"):
            raise ValueError("route paths start with /: " + pattern)
        self.methods = methods
        self.pattern = pattern
        self.handler = handler
        self.segments = []
        for part in _split(pattern):
            if part.startswith("{") and part.endswith("}"):
                name, kind = part[1:-1], "str"
                if ":" in name:
                    name, kind = name.split(":", 1)
                if kind not in _CONVERTERS:
                    raise ValueError("unknown path parameter type: " + kind)
                self.segments.append((name, kind))
            else:
                self.segments.append((None, part))

    def match(self, path):
        """Return the path parameters when the path matches, else None."""
        parts = _split(path)
        params = {}
        for i, (name, value) in enumerate(self.segments):
            if name is not None and value == "path":
                if i >= len(parts):
                    return None
                params[name] = "/".join(parts[i:])
                return params
            if i >= len(parts):
                return None
            if name is None:
                if parts[i] != value:
                    return None
                continue
            try:
                params[name] = _CONVERTERS[value](_unquote(parts[i]))
            except ValueError:
                return None
        if len(parts) != len(self.segments):
            return None
        return params


def _split(path):
    """Split a path into its non-empty segments."""
    return [part for part in path.split("/") if part]


def _unquote(text):
    """Decode percent-escapes in a path segment."""
    if "%" not in text:
        return text
    out = bytearray()
    i = 0
    raw = text.encode()
    while i < len(raw):
        if raw[i] == 37 and i + 2 < len(raw):  # %XX
            try:
                out.append(int(raw[i + 1 : i + 3].decode(), 16))
                i += 3
                continue
            except ValueError:
                pass
        out.append(raw[i])
        i += 1
    return out.decode()


class App:
    """A set of routes, the code to run once per worker, and shared state."""

    def __init__(self):
        """Start with no routes and empty state."""
        self.routes = []
        self.state = State()
        self._boot = []
        self._booted = False

    def route(self, path, methods=("GET",)):
        """Register a handler for a path and a list of methods."""

        def register(handler):
            """Add the handler and return it unchanged."""
            upper = [m.upper() for m in methods]
            self.routes.append(Route(upper, path, handler))
            return handler

        return register

    def get(self, path):
        """Register a GET handler."""
        return self.route(path, ("GET",))

    def post(self, path):
        """Register a POST handler."""
        return self.route(path, ("POST",))

    def put(self, path):
        """Register a PUT handler."""
        return self.route(path, ("PUT",))

    def patch(self, path):
        """Register a PATCH handler."""
        return self.route(path, ("PATCH",))

    def delete(self, path):
        """Register a DELETE handler."""
        return self.route(path, ("DELETE",))

    def on_boot(self, fn):
        """Run fn once per worker before the first request: the place for secrets."""
        self._boot.append(fn)
        return fn

    async def boot(self):
        """Run the on_boot hooks, once."""
        if self._booted:
            return
        for fn in self._boot:
            await _maybe_await(fn())
        self._booted = True

    async def handle(self, request):
        """Route a request to its handler and return a Response."""
        request.app = self
        allowed = []
        for route in self.routes:
            params = route.match(request.path)
            if params is None:
                continue
            if request.method not in route.methods:
                allowed.extend(route.methods)
                continue
            request.params = params
            try:
                await self.boot()
                return _to_response(
                    await _maybe_await(route.handler(request, **params))
                )
            except HTTPError as error:
                return _error_response(error)
        if allowed:
            return _error_response(
                HTTPError(405, headers={"allow": ", ".join(allowed)})
            )
        return _error_response(HTTPError(404))


async def dispatch(app, raw):
    """Handle one request from the shim: JSON in, JSON out.

    Unexpected errors become a 500 for the caller, with the traceback in "log" for
    the function's logs. Background tasks are kept under "bg" until the shim runs
    them with run_background.
    """
    data = json.loads(raw)
    request = Request(
        data.get("method", "GET"),
        data.get("path", "/"),
        data.get("query"),
        data.get("headers"),
        data.get("body", ""),
    )
    log = None
    try:
        response = await app.handle(request)
    except Exception as error:
        log = format_exception(error)
        response = _error_response(HTTPError(500))
    wire = response.wire()
    wire["log"] = log
    wire["bg"] = None
    if request._background:
        _next_id[0] += 1
        _pending[_next_id[0]] = request._background
        wire["bg"] = _next_id[0]
    return json.dumps(wire)


async def run_background(task_id):
    """Run a request's background tasks; return their tracebacks, if any, joined."""
    logs = []
    for task in _pending.pop(task_id, []):
        try:
            await _maybe_await(task)
        except Exception as error:
            logs.append(format_exception(error))
    return "\n".join(logs)
