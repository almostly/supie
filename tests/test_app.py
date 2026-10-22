"""Routing, requests, responses and errors, under CPython through TestClient."""

import base64
import json

import pytest

from supie import App, HTTPError, Response, env
from supie.testing import TestClient


def make_app():
    """An app exercising every kind of handler and return value."""
    app = App()

    @app.on_boot
    def setup():
        """Read a secret once per worker."""
        app.state.key = env["SCORING_KEY"]
        app.state.boots = getattr(app.state, "boots", 0) + 1

    @app.get("/")
    def root(req):
        """Return a dict, which becomes JSON."""
        return {"ok": True, "key": req.state.key}

    @app.get("/loans/{loan_id:int}")
    async def loan(req, loan_id):
        """Use an int path parameter in an async handler."""
        return {"loan_id": loan_id, "type": type(loan_id).__name__}

    @app.get("/files/{rest:path}")
    def files(req, rest):
        """Capture the rest of the path."""
        return {"rest": rest}

    @app.get("/users/{name}")
    def user(req, name):
        """Decode a percent-encoded string parameter."""
        return {"name": name}

    @app.post("/score")
    def score(req):
        """Validate a JSON body."""
        body = req.json()
        if "age" not in body:
            raise HTTPError(422, "missing: age")
        return Response(
            {"score": body["age"] * 2}, status=201, headers={"X-Model": "v1"}
        )

    @app.get("/text")
    def text(req):
        """Return a string, which becomes text."""
        return "plain"

    @app.get("/bytes")
    def raw(req):
        """Return bytes, which stay binary."""
        return Response(b"\x00\x01\xff", media_type="application/x-thing")

    @app.delete("/nothing")
    def nothing(req):
        """Return None, which becomes 204."""

    @app.get("/boom")
    def boom(req):
        """Fail unexpectedly."""
        raise ValueError("kaboom")

    @app.get("/whoami")
    def whoami(req):
        """Return the caller's JWT claims."""
        return {"user": req.user}

    @app.post("/later")
    def later(req):
        """Queue background work."""

        async def work():
            """Record that the background task ran."""
            app.state.done = True

        req.background(work())
        return {"queued": True}

    return app


@pytest.fixture
def client():
    """A test client with the secret the app reads at boot."""
    return TestClient(make_app(), env={"SCORING_KEY": "s3cret"})


def test_json_and_boot(client):
    """A dict becomes JSON, and on_boot runs once across requests."""
    first = client.get("/")
    assert first.status == 200
    assert first.headers["content-type"] == "application/json"
    assert first.json() == {"ok": True, "key": "s3cret"}
    client.get("/")
    assert client.app.state.boots == 1


def test_path_parameters(client):
    """Typed, path and percent-encoded parameters."""
    assert client.get("/loans/42").json() == {"loan_id": 42, "type": "int"}
    assert client.get("/loans/abc").status == 404
    assert client.get("/files/a/b/c.txt").json() == {"rest": "a/b/c.txt"}
    assert client.get("/users/Ada%20L").json() == {"name": "Ada L"}


def test_response_and_http_error(client):
    """Response sets status and headers; HTTPError becomes a JSON error."""
    ok = client.post("/score", json={"age": 30})
    assert (ok.status, ok.json(), ok.headers["x-model"]) == (201, {"score": 60}, "v1")
    bad = client.post("/score", json={})
    assert (bad.status, bad.json()) == (422, {"error": "missing: age"})


def test_bad_json(client):
    """A missing or broken body is a 400."""
    assert client.post("/score").status == 400
    assert client.post("/score", body="{nope").json() == {"error": "invalid JSON body"}


def test_text_bytes_and_empty(client):
    """Strings are text, bytes stay binary, None is 204."""
    text = client.get("/text")
    assert (text.text, text.headers["content-type"]) == (
        "plain",
        "text/plain; charset=utf-8",
    )
    raw = client.get("/bytes")
    assert (raw.content, raw.headers["content-type"]) == (
        b"\x00\x01\xff",
        "application/x-thing",
    )
    assert client.delete("/nothing").status == 204


def test_not_found_and_method_not_allowed(client):
    """Unknown paths are 404; known paths with the wrong method are 405."""
    assert client.get("/missing").status == 404
    wrong = client.get("/score")
    assert (wrong.status, wrong.headers["allow"]) == (405, "POST")


def test_unexpected_error():
    """An unexpected error is a 500 for the caller, with the traceback logged."""
    quiet = TestClient(make_app(), env={"SCORING_KEY": "x"}, raise_errors=False)
    response = quiet.get("/boom")
    assert (response.status, response.json()) == (
        500,
        {"error": "Internal Server Error"},
    )
    assert "ValueError: kaboom" in response.log


def test_unexpected_error_fails_tests(client):
    """By default, TestClient surfaces the traceback instead of a 500."""
    with pytest.raises(AssertionError, match="kaboom"):
        client.get("/boom")


def test_user_claims(client):
    """req.user decodes the bearer token's claims."""
    claims = {"sub": "user-1", "role": "authenticated"}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    token = f"eyJhbGciOiJIUzI1NiJ9.{payload}.signature"
    response = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert response.json() == {"user": claims}
    assert client.get("/whoami").json() == {"user": None}


def test_background(client):
    """Background tasks run after the response."""
    assert client.post("/later").json() == {"queued": True}
    assert client.app.state.done is True


def test_missing_secret():
    """A secret that isn't set fails boot with a KeyError, logged as a 500."""
    response = TestClient(make_app(), raise_errors=False).get("/")
    assert response.status == 500
    assert "KeyError" in response.log


def test_bad_route():
    """Routes must start with / and use known parameter types."""
    app = App()
    with pytest.raises(ValueError):
        app.get("nope")(lambda req: None)
    with pytest.raises(ValueError):
        app.get("/x/{y:uuid}")(lambda req, y: None)


def test_compare_digest():
    """Constant-time comparison, for str and bytes, including the fallback."""
    from supie import compare_digest
    from supie.app import compare_digest as runtime_compare

    assert compare_digest("abc", "abc") and compare_digest(b"abc", "abc")
    assert not compare_digest("abc", "abd")
    assert not compare_digest("abc", "abcd") and not compare_digest("", "a")
    assert runtime_compare is compare_digest
