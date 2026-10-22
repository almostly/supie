"""Tests for the __NAME__ function, under plain Python."""

from supie.testing import TestClient

from main import app

client = TestClient(app)


def test_hello() -> None:
    """GET / greets the caller."""
    assert client.get("/?name=Ada").json() == {"hello": "Ada"}


def test_echo_needs_an_object() -> None:
    """POST /echo refuses JSON that isn't an object."""
    assert client.post("/echo", json=[1, 2]).status == 422
