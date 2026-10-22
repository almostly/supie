"""The Supabase client, against a fake fetch."""

import pytest

from supie import App
from supie.supabase import SupabaseError, quote
from supie.testing import FakeResponse, TestClient


def test_quote():
    """Unreserved characters stay; everything else is percent-encoded."""
    assert quote("a b/c~") == "a%20b%2Fc~"
    assert quote("a/b", safe="/") == "a/b"
    assert quote("é") == "%C3%A9"


def run(handler, fetch, env=None, headers=None):
    """Run one async handler through a TestClient with a fake fetch."""
    app = App()
    app.get("/")(handler)
    client = TestClient(app, env=env, fetch=fetch)
    return client, client.get("/", headers=headers)


def test_select_as_caller():
    """req.supabase forwards the caller's token and builds PostgREST filters."""

    async def handler(req):
        """Select loans with several filters."""
        result = await (
            req.supabase.table("loans")
            .select("id, amount", count="exact")
            .eq("status", "open")
            .gte("amount", 1000)
            .in_("region", ["eu", "a,b"])
            .is_("deleted_at", None)
            .order("amount", desc=True)
            .limit(10)
            .execute()
        )
        return {"data": result.data, "count": result.count}

    def fetch(call):
        """Answer with two rows and a total count."""
        return FakeResponse([{"id": 1}, {"id": 2}], headers={"Content-Range": "0-1/7"})

    client, response = run(handler, fetch, headers={"Authorization": "Bearer user-jwt"})
    assert response.json() == {"data": [{"id": 1}, {"id": 2}], "count": 7}
    call = client.calls[0]
    assert call.method == "GET"
    assert call.url == (
        "https://test.supabase.co/rest/v1/loans?select=id,%20amount&status=eq.open"
        "&amount=gte.1000&region=in.(eu,%22a,b%22)&deleted_at=is.null"
        "&order=amount.desc&limit=10"
    )
    assert call.headers["authorization"] == "Bearer user-jwt"
    assert call.headers["apikey"] == "anon-key"
    assert call.headers["prefer"] == "count=exact"


def test_insert_as_admin():
    """req.supabase_admin uses the service role and asks for the inserted rows."""

    async def handler(req):
        """Insert one row."""
        result = (
            await req.supabase_admin.table("score_log").insert({"pd": 0.1}).execute()
        )
        return result.data

    client, response = run(
        handler, lambda call: FakeResponse([{"id": 9, "pd": 0.1}], 201)
    )
    assert response.json() == [{"id": 9, "pd": 0.1}]
    call = client.calls[0]
    assert (call.method, call.json()) == ("POST", {"pd": 0.1})
    assert call.headers["authorization"] == "Bearer service-role-key"
    assert call.headers["prefer"] == "return=representation"


def test_upsert_update_delete_single():
    """Writes set the method, Prefer header and filters."""

    async def handler(req):
        """Run one of each write."""
        db = req.supabase_admin
        await db.table("t").upsert([{"id": 1}], on_conflict="id").execute()
        await db.table("t").update({"x": 2}).eq("id", 1).execute()
        await db.table("t").delete().eq("id", 1).execute()
        await db.table("t").select().eq("id", 1).single().execute()
        return {}

    client, _ = run(handler, lambda call: FakeResponse({}))
    upsert, update, delete, single = client.calls
    assert upsert.url.endswith("/rest/v1/t?on_conflict=id")
    assert (
        upsert.headers["prefer"] == "return=representation,resolution=merge-duplicates"
    )
    assert (update.method, update.url.split("?")[1]) == ("PATCH", "id=eq.1")
    assert (delete.method, delete.body) == ("DELETE", None)
    assert single.headers["accept"] == "application/vnd.pgrst.object+json"
    assert single.url.endswith("?select=*&id=eq.1")


def test_rpc_and_errors():
    """rpc posts the parameters; an error response raises SupabaseError."""
    seen = {}

    async def handler(req):
        """Call a function, then catch a failing query."""
        result = await req.supabase.rpc("score_applicant", {"id": 3})
        seen["rpc"] = result.data
        try:
            await req.supabase.table("secret").select().execute()
        except SupabaseError as error:
            return {"status": error.status, "message": error.message}

    def fetch(call):
        """Answer the rpc, refuse the select."""
        if "/rpc/" in call.url:
            assert call.json() == {"id": 3}
            return FakeResponse(0.42)
        return FakeResponse({"message": "permission denied for table secret"}, 403)

    _, response = run(handler, fetch)
    assert seen["rpc"] == 0.42
    assert response.json() == {
        "status": 403,
        "message": "permission denied for table secret",
    }


def test_functions_and_storage():
    """functions.invoke and Storage download and upload."""

    async def handler(req):
        """Call another function and round-trip a file."""
        fraud = await req.supabase.functions.invoke("fraud-check", {"id": 1})
        bucket = req.supabase_admin.storage.from_("models")
        await bucket.upload("v1/model.bin", b"\x00\xff", upsert=True)
        data = await bucket.download("v1/model.bin")
        return {"fraud": fraud, "size": len(data), "first": data[0], "last": data[-1]}

    def fetch(call):
        """Answer each endpoint."""
        if "/functions/v1/" in call.url:
            return FakeResponse({"flag": False})
        if call.method == "POST":
            assert call.body == b"\x00\xff"
            assert call.headers["x-upsert"] == "true"
            return FakeResponse({"Key": "models/v1/model.bin"})
        return FakeResponse(
            b"\x00\xff", headers={"content-type": "application/octet-stream"}
        )

    client, response = run(handler, fetch)
    assert response.json() == {
        "fraud": {"flag": False},
        "size": 2,
        "first": 0,
        "last": 255,
    }
    assert (
        client.calls[1].url
        == "https://test.supabase.co/storage/v1/object/models/v1/model.bin"
    )


def test_missing_supabase_env():
    """Without SUPABASE_URL the handler fails with a clear KeyError."""

    async def handler(req):
        """Touch the client."""
        return req.supabase.url

    app = App()
    app.get("/")(handler)
    client = TestClient(app, raise_errors=False)
    client.host.vars.pop("SUPABASE_URL")
    log = client.get("/").log
    assert log and "KeyError" in log


@pytest.mark.parametrize(
    "value, expected", [(True, "true"), (None, "null"), (1.5, "1.5")]
)
def test_filter_values(value, expected):
    """Booleans, None and numbers are formatted for PostgREST."""
    from supie.supabase import _value

    assert _value(value) == expected


class RateLimitError(Exception):
    """Stands in for the error Supabase's fetch throws over the per-trace limit."""


def test_rate_limit_and_network_errors():
    """A thrown RateLimitError becomes RateLimited; other throws become SupabaseError(0)."""
    from supie.supabase import RateLimited

    async def handler(req):
        """Invoke twice: once rate limited, once failing to connect."""
        out = {}
        try:
            await req.supabase.functions.invoke("busy", {})
        except RateLimited as error:
            out["limited"] = [error.status, error.retry_after]
        try:
            await req.supabase.functions.invoke("down", {})
        except SupabaseError as error:
            out["down"] = [error.status, error.message]
        return out

    def fetch(call):
        """Throw as Supabase's fetch would."""
        if call.url.endswith("/busy"):
            raise RateLimitError(
                "Rate limit exceeded for trace abc. Retry after 59971ms."
            )
        raise ConnectionError("connection refused")

    _, response = run(handler, fetch)
    assert response.json() == {
        "limited": [429, 59.971],
        "down": [0, "ConnectionError: connection refused"],
    }
