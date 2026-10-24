"""Token checks with Supabase Auth: caching, expiry, merging and fail-closed limits."""

import base64
import json

import pytest

from supie import app as runtime
from supie import gather, sleep
from supie.testing import FakeResponse, run

ALICE = {"role": "authenticated", "sub": "alice"}


def jwt(claims):
    """An unsigned token with these claims."""
    part = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"x.{part}.sig"


class Auth:
    """A fake Supabase Auth that counts calls and answers with a fixed status."""

    def __init__(self, status=200, user_id="alice"):
        """Answer every call with this status and user."""
        self.status = status
        self.user_id = user_id
        self.calls = 0

    def __call__(self, call):
        """One Auth call."""
        self.calls += 1
        if self.status == 0:
            raise ConnectionError("auth is down")
        return FakeResponse({"id": self.user_id}, self.status)


@pytest.fixture
def clock(monkeypatch):
    """A clock the test moves by hand."""
    now = [1_000_000.0]
    monkeypatch.setattr(runtime, "_clock", lambda: now[0])
    return now


def test_a_rejection_is_remembered_for_a_minute(clock):
    """A token Auth refused isn't checked again for 60 s, then it is."""
    auth = Auth(status=401)
    token = jwt({"role": "service_role"})

    async def scenario():
        """Four checks, then one after the minute."""
        states = [(await runtime._lookup(token, "service", {}))[0] for _ in range(4)]
        calls = auth.calls
        clock[0] += 61
        states.append((await runtime._lookup(token, "service", {}))[0])
        return states, calls

    states, calls = run(scenario(), fetch=auth)
    assert states == ["invalid"] * 5
    assert calls == 1 and auth.calls == 2


def test_a_verified_token_is_checked_again_after_it_expires(clock):
    """A verified user token is trusted until its exp, then checked again."""
    auth = Auth()
    claims = {**ALICE, "exp": clock[0] + 10}
    token = jwt(claims)

    async def scenario():
        """Twice before exp, once after."""
        first = await runtime._lookup(token, "user", claims)
        second = await runtime._lookup(token, "user", claims)
        calls = auth.calls
        clock[0] += 11
        third = await runtime._lookup(token, "user", claims)
        return [first[0], second[0], third[0]], calls

    states, calls = run(scenario(), fetch=auth)
    assert states == ["ok", "ok", "ok"]
    assert calls == 1 and auth.calls == 2


def test_trust_lasts_at_most_an_hour(clock):
    """A token with a far exp is checked again after an hour."""
    auth = Auth()
    claims = {**ALICE, "exp": clock[0] + 86_400}
    token = jwt(claims)

    async def scenario():
        """Once, then after an hour."""
        await runtime._lookup(token, "user", claims)
        clock[0] += 3601
        await runtime._lookup(token, "user", claims)

    run(scenario(), fetch=auth)
    assert auth.calls == 2


def test_a_user_must_match_the_tokens_subject():
    """Auth loading a different user than the token's sub counts as invalid."""
    auth = Auth(user_id="bob")
    assert run(runtime._lookup(jwt(ALICE), "user", ALICE), fetch=auth)[0] == "invalid"


def test_auth_errors_are_never_cached():
    """A 5xx, a 429 or an unreachable Auth fails closed for that check only."""
    for status in (500, 429, 0):
        auth = Auth(status=status)
        token = jwt(ALICE)

        async def scenario():
            """Two checks in a row."""
            first = await runtime._lookup(token, "user", ALICE)
            second = await runtime._lookup(token, "user", ALICE)
            return first[0], second[0]

        assert run(scenario(), fetch=auth) == ("unavailable", "unavailable")
        assert auth.calls == 2, status


def test_concurrent_checks_of_one_token_share_one_call():
    """A check that finds the token already being checked waits for it."""
    auth = Auth()
    token = jwt(ALICE)

    async def scenario():
        """One check finishes while another waits on it."""
        runtime._in_flight[token] = True

        async def finish():
            """The first check completes."""
            await sleep(0.1)
            runtime._users[token] = (runtime._clock() + 60, None)
            del runtime._in_flight[token]

        results = []

        async def wait():
            """The second check."""
            results.append(await runtime._lookup(token, "user", ALICE))

        await gather(wait(), finish())
        return results[0][0]

    assert run(scenario(), fetch=auth) == "ok"
    assert auth.calls == 0


def test_too_many_checks_at_once_fail_closed():
    """Past the in-flight limit, a new token isn't checked at all."""
    auth = Auth()

    async def scenario():
        """Fill the limit with other tokens, then check one more."""
        for n in range(runtime._MAX_IN_FLIGHT):
            runtime._in_flight[f"other-{n}"] = True
        return (await runtime._lookup(jwt(ALICE), "user", ALICE))[0]

    assert run(scenario(), fetch=auth) == "unavailable"
    assert auth.calls == 0


def test_caches_stay_bounded(clock):
    """A full cache evicts expired entries first, then the oldest."""
    cache = {}
    for n in range(3):
        runtime._remember(cache, f"t{n}", clock[0] + 100, None, 3)
    runtime._remember(cache, "t3", clock[0] + 100, None, 3)
    assert list(cache) == ["t1", "t2", "t3"]
    cache["t1"] = (clock[0] - 1, None)
    runtime._remember(cache, "t4", clock[0] + 100, None, 3)
    assert set(cache) == {"t2", "t3", "t4"}
