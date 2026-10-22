"""Supabase's Postgres and PostgREST in Docker, for the postgres and pgflow tests."""

import base64
import contextlib
import hashlib
import hmac
import io
import json
import shutil
import socket
import subprocess
import tarfile
import time
import urllib.error
import urllib.request
import uuid

import pytest

from supie import runtimes
from supie.testing import FakeResponse

SECRET = "super-secret-jwt-token-with-at-least-32-characters"
PGFLOW = runtimes.Runtime(
    name="pgflow-core",
    package="@pgflow/core",
    version="0.17.2",
    integrity="sha512-XegzRe/HFkv0o15FQvPWNLxAk8HEu8f2XazkGHJQeHidIPqYTrC1j9r8CXTmU6AuuDgz5sUzddoizHbMSDokoA==",
    files=(),
)


def jwt(claims):
    """An HS256 JWT signed with the test secret."""

    def part(data):
        """Base64url-encode JSON without padding."""
        raw = json.dumps(data, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    head = part({"alg": "HS256", "typ": "JWT"}) + "." + part(claims)
    sig = hmac.new(SECRET.encode(), head.encode(), hashlib.sha256).digest()
    return head + "." + base64.urlsafe_b64encode(sig).decode().rstrip("=")


SERVICE = jwt({"role": "service_role"})
ANON = jwt({"role": "anon"})


def docker(*args, **kwargs):
    """Run a docker command, failing the test with Docker's error if it fails."""
    result = subprocess.run(["docker", *args], capture_output=True, text=True, **kwargs)
    if result.returncode:
        pytest.fail(f"docker {args[0]} failed:\n{result.stderr}")
    return result


def psql(db, sql, user="postgres", transaction=False):
    """Run SQL in the test database and return psql's unaligned output."""
    args = ["exec", "-i", db, "psql", "-U", user, "-d", "postgres"]
    if user == "postgres":
        args += ["-h", "localhost"]
    args += ["-v", "ON_ERROR_STOP=1", "-qAt"]
    if transaction:
        args.append("-1")
    return docker(*args, input=sql).stdout.strip()


def pgflow_migrations():
    """pgflow's migrations, from its npm package, in order."""
    target = runtimes.cache_dir() / "pgflow" / PGFLOW.version
    if not target.is_dir():
        with urllib.request.urlopen(PGFLOW.tarball, timeout=120) as response:
            data = response.read()
        assert runtimes.verify(data, PGFLOW.integrity), (
            "pgflow failed its integrity check"
        )
        partial = target.with_name(target.name + ".partial")
        shutil.rmtree(partial, ignore_errors=True)
        partial.mkdir(parents=True)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            for member in archive.getmembers():
                if "/supabase/migrations/" in member.name and member.name.endswith(
                    ".sql"
                ):
                    name = member.name.rsplit("/", 1)[1]
                    content = archive.extractfile(member)
                    assert content is not None, member.name
                    (partial / name).write_bytes(content.read())
        partial.rename(target)
    return sorted(target.glob("*.sql"))


# Supabase's Realtime service provides realtime.send in a real project; the bare
# Postgres image has the schema but not the function pgflow's migrations call.
REALTIME_STUB = """
create or replace function realtime.send(
  payload jsonb, event text, topic text, private boolean default true
) returns void language sql as $$ select $$;
grant execute on function realtime.send(jsonb, text, text, boolean) to postgres;
"""


def _free_port():
    """A free local TCP port."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Stack(tuple):
    """(base, db), plus the Docker network and the PostgREST container's name."""

    def __new__(cls, base, db, network, api):
        """Unpack like (base, db), with the rest as attributes."""
        stack = super().__new__(cls, (base, db))
        stack.network = network
        stack.api = api
        return stack


@contextlib.contextmanager
def postgres_rest(images, setup):
    """Postgres, prepared by setup(db), with PostgREST in front; yields a Stack."""
    if not shutil.which("docker") or subprocess.call(
        ["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    ):
        pytest.skip("needs Docker")
    tag = uuid.uuid4().hex[:8]
    network, db, api = f"supie-net-{tag}", f"supie-pg-{tag}", f"supie-rest-{tag}"
    port = _free_port()
    docker("network", "create", network)
    try:
        docker(
            "run", "-d", "--name", db, "--network", network,
            "-e", "POSTGRES_PASSWORD=postgres", images["postgres"],
        )  # fmt: skip
        for _ in range(120):
            ready = subprocess.run(
                ["docker", "exec", db, "psql", "-U", "postgres", "-h", "localhost",
                 "-tAc", "select 1 from pg_roles where rolname = 'service_role'"],
                capture_output=True, text=True,
            )  # fmt: skip
            if ready.stdout.strip() == "1":
                break
            time.sleep(0.5)
        setup(db)
        docker(
            "run", "-d", "--name", api, "--network", network,
            "-p", f"127.0.0.1:{port}:3000",
            "-e", f"PGRST_DB_URI=postgres://postgres:postgres@{db}:5432/postgres",
            "-e", "PGRST_DB_SCHEMAS=public", "-e", "PGRST_DB_ANON_ROLE=anon",
            "-e", f"PGRST_JWT_SECRET={SECRET}", images["postgrest"],
        )  # fmt: skip
        base = f"http://127.0.0.1:{port}"
        for _ in range(150):  # 503 until PostgREST has loaded the schema cache
            try:
                urllib.request.urlopen(base + "/", timeout=2)
                break
            except OSError:
                time.sleep(0.2)
        else:
            pytest.fail("PostgREST didn't become ready")
        yield Stack(base, db, network, api)
    finally:
        subprocess.run(["docker", "rm", "-f", api, db], capture_output=True)
        subprocess.run(["docker", "network", "rm", network], capture_output=True)


def proxy(base):
    """A fetch that sends the Supabase client's REST calls to the local PostgREST."""

    def fetch(call):
        """Forward one call and return PostgREST's answer."""
        url = call.url.replace("https://test.supabase.co/rest/v1", base)
        data = call.body.encode() if isinstance(call.body, str) else call.body
        request = urllib.request.Request(
            url, data=data, method=call.method, headers=call.headers
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return FakeResponse(
                    response.read().decode(), response.status, dict(response.headers)
                )
        except urllib.error.HTTPError as error:
            return FakeResponse(error.read().decode(), error.code, dict(error.headers))

    return fetch
