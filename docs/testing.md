# Testing

Handlers are plain Python, so you test them with pytest and no Supabase at all:

```python
from supie.testing import FakeResponse, TestClient

from main import app


def test_score():
    client = TestClient(app, env={"SCORING_KEY": "test"}, fetch=lambda call: FakeResponse([]))
    response = client.post("/score", json={"age": 40})
    assert response.status == 201
    assert client.calls == []  # every request the handler made through Supabase
```

To test a flow outside a request, `supie.testing.run(flow.run(input), sleep_scale=0)` binds a fake host and skips retry delays.

`TestClient` runs background tasks after each response. Its `fetch` answers the Supabase client's requests, and `client.calls` records them. By default, an unexpected error fails the test with its traceback instead of returning a 500.

Tests run under CPython. A handler that passes them can still use something MicroPython lacks, so run `supie serve` before you deploy, or choose Pyodide.
