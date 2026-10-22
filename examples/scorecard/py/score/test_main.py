"""Tests for the scorecard, under plain Python."""

from supie.testing import FakeResponse, TestClient

from main import app


def client():
    """A test client whose fake Supabase accepts every insert."""
    return TestClient(app, fetch=lambda call: FakeResponse([{"id": 1}], 201))


def test_score():
    """A complete applicant gets a score and a PD between 0 and 1."""
    c = client()
    response = c.post("/", json={"id": 7, "age": 40, "income": 250_000, "dti": 0.3})
    assert response.status == 200
    assert response.json()["score"] == 621
    assert 0 < response.json()["pd"] < 1


def test_decision_is_logged():
    """The decision goes to score_log after the response."""
    c = client()
    c.post("/", json={"id": 7, "age": 40, "income": 250_000, "dti": 0.3})
    (insert,) = c.calls
    assert insert.url.endswith("/rest/v1/score_log")
    assert insert.json()["applicant_id"] == 7


def test_missing_fields():
    """Missing features are a 422 that names them."""
    response = client().post("/", json={"age": 40})
    assert response.status == 422
    assert response.json() == {"error": "missing: dti, income"}
