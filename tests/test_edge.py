"""Functions built by supie, running on Supabase's Edge Runtime image (Docker).

Each test runs against the MicroPython and the Pyodide build of the same
function, under the hosted CPU and memory limits.
"""

import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

from supie import build

pytestmark = [pytest.mark.edge, pytest.mark.network]

HERE = Path(__file__).parent / "edge"
FUNCTIONS = ["demo", "demo-pyodide"]


def _free_port():
    """A free local TCP port."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def edge(tmp_path_factory, images):
    """Build the demo function both ways and serve them from the Edge Runtime."""
    if not shutil.which("docker") or subprocess.call(
        ["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    ):
        pytest.skip("needs Docker")
    root = tmp_path_factory.mktemp("edge")
    (root / "supabase").mkdir()
    (root / "supabase" / "config.toml").write_text('project_id = "edge"\n')
    for name in FUNCTIONS:
        shutil.copytree(HERE / "demo", root / "py" / name)
    (root / "supie.toml").write_text('[functions.demo-pyodide]\nruntime = "pyodide"\n')
    shutil.copytree(HERE / "echo", root / "supabase" / "functions" / "echo")
    project = build.load_project(root)
    for name in FUNCTIONS:
        build.build(project, project.functions[name])

    port = _free_port()
    container = f"supie-test-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            container,
            "-p",
            f"127.0.0.1:{port}:9000",
            "-v",
            f"{root / 'supabase' / 'functions'}:/home/deno/functions:ro",
            "-v",
            f"{HERE / 'main'}:/home/deno/main:ro",
            images["edge"],
            "start",
            "--main-service",
            "/home/deno/main",
        ],
        check=True,
        capture_output=True,
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(150):
        try:
            urllib.request.urlopen(f"{base}/echo", timeout=2)
            break
        except OSError:
            time.sleep(0.2)
    else:
        output = subprocess.run(
            ["docker", "logs", container], capture_output=True, text=True
        )
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)
        pytest.fail("the Edge Runtime didn't start:\n" + output.stdout + output.stderr)
    yield base, container
    if os.environ.get("SUPIE_KEEP_CONTAINER"):
        return
    subprocess.run(["docker", "rm", "-f", container], capture_output=True)


def call(edge, method, path, body=None, headers=None):
    """Send a request; return status, headers and the raw body."""
    base, _ = edge
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        base + path, data=data, method=method, headers=headers or {}
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read()


def logs(edge):
    """The container's logs so far."""
    _, container = edge
    result = subprocess.run(
        ["docker", "logs", container], capture_output=True, text=True
    )
    return result.stdout + result.stderr


@pytest.mark.parametrize("fn", FUNCTIONS)
def test_json_boot_and_query(edge, fn):
    """A cold worker boots, runs on_boot and answers JSON."""
    status, headers, body = call(
        edge, "GET", f"/{fn}?name=Ada", headers={"x-cold": "1"}
    )
    assert status == 200, body
    assert headers["content-type"] == "application/json"
    assert json.loads(body) == {"greeting": "hej", "name": "Ada"}


@pytest.mark.parametrize("fn", FUNCTIONS)
def test_score(edge, fn):
    """Path parameters, a JSON body, helper modules and errors."""
    status, _, body = call(edge, "POST", f"/{fn}/score/12", {"age": 40})
    assert status == 201, body
    assert json.loads(body) == {"applicant": 12, "score": 612, "pd": 0.3975}
    status, _, body = call(edge, "POST", f"/{fn}/score/12", {})
    assert (status, json.loads(body)) == (422, {"error": "missing: age"})
    assert call(edge, "GET", f"/{fn}/score/12")[0] == 405
    assert call(edge, "GET", f"/{fn}/nope")[0] == 404


@pytest.mark.parametrize("fn", FUNCTIONS)
def test_supabase_client(edge, fn):
    """The Supabase client reaches the (fake) project with the right token."""
    status, _, body = call(
        edge, "GET", f"/{fn}/db", headers={"Authorization": "Bearer caller"}
    )
    assert status == 200, body
    result = json.loads(body)
    assert result["count"] == 3
    assert result["select"]["path"] == "/echo/rest/v1/loans"
    assert result["select"]["query"] == "?select=id&id=eq.7"
    assert result["select"]["authorization"] == "Bearer caller"
    assert result["select"]["prefer"] == "count=exact"
    assert result["rpc"]["authorization"] == "Bearer service-role-key"
    assert bytes(result["rpc"]["body"]) == b'{"x": 1}'


@pytest.mark.parametrize("fn", FUNCTIONS)
def test_binary(edge, fn):
    """Binary data both ways: Storage download and upload, and a bytes response."""
    status, _, body = call(edge, "GET", f"/{fn}/storage")
    assert status == 200, body
    assert json.loads(body) == {
        "downloaded": [0, 1, 254, 255],
        "uploaded": [0, 1, 254, 255],
    }
    status, headers, body = call(edge, "GET", f"/{fn}/bytes")
    assert (status, headers["content-type"], body) == (
        200,
        "application/x-demo",
        b"\x00\x80\xff",
    )


@pytest.mark.parametrize("fn", FUNCTIONS)
def test_background_and_errors(edge, fn):
    """Background tasks run after the response; errors are 500s with a logged traceback."""
    assert json.loads(call(edge, "POST", f"/{fn}/later")[2]) == {"queued": True}
    status, _, body = call(edge, "GET", f"/{fn}/boom")
    assert (status, json.loads(body)) == (500, {"error": "Internal Server Error"})
    for _ in range(20):
        text = logs(edge)
        if "background done" in text and "ZeroDivisionError" in text:
            break
        time.sleep(0.2)
    assert "background done" in text
    assert "ZeroDivisionError" in text
    assert '"event":"supie.boot"' in text


@pytest.mark.parametrize("fn", FUNCTIONS)
def test_flow_inline(edge, fn):
    """A flow runs in the request: parallel checks, a retry, and a branch."""
    status, _, body = call(edge, "POST", f"/{fn}/decide", {"id": 1})
    assert status == 200, body
    result = json.loads(body)
    assert result["output"] == {"decision": "approve", "attempts": 2}
    assert result["seconds"] < 0.39  # the two 0.2 s checks overlapped
    status, _, body = call(edge, "POST", f"/{fn}/decide", {"id": 2, "suspicious": True})
    assert json.loads(body)["output"] == {"decision": "refer"}


@pytest.mark.parametrize("fn", FUNCTIONS)
def test_flow_mounted(edge, fn):
    """POST starts a run in the background; GET shows it finished."""
    status, headers, body = call(edge, "POST", f"/{fn}/flows/scoring", {"id": 3})
    assert status == 202, body
    run_id = json.loads(body)["run_id"]
    assert len(run_id) == 32
    for _ in range(50):
        status, _, body = call(edge, "GET", f"/{fn}/flows/scoring/{run_id}")
        if json.loads(body)["status"] != "running":
            break
        time.sleep(0.1)
    state = json.loads(body)
    assert state["status"] == "succeeded", state
    assert set(state["steps"]) == {"bureau", "fraud", "score", "decide"}
