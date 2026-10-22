"""A small async Supabase client over the shim's fetch, modeled on supabase-py.

supabase-py can't run in the function (no sockets under WebAssembly), so this covers
the common calls: table queries, RPC, Edge Function calls and Storage. It keeps to
the subset of Python that supie.app uses, so it runs under MicroPython too.
"""

import json

from supie.app import get_host

_UNRESERVED = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"


class SupabaseError(Exception):
    """A Supabase API call failed; status and message come from the response."""

    def __init__(self, status, message, details=None):
        """Store the HTTP status, the error message and the raw error body."""
        super().__init__(str(status) + ": " + str(message))
        self.status = status
        self.message = message
        self.details = details


class RateLimited(SupabaseError):
    """Supabase refused a call between functions: too many in one request trace.

    Hosted Supabase allows 30 calls from functions to functions per trace and
    minute; PostgREST, Storage and other hosts don't count. retry_after is in
    seconds.
    """

    def __init__(self, message, retry_after=None):
        """Store the message and how long to wait, when Supabase says."""
        super().__init__(429, message)
        self.retry_after = retry_after


def _retry_after(message):
    """Parse "Retry after 59971ms" into seconds, or None."""
    marker = "Retry after "
    if marker not in message:
        return None
    digits = ""
    for char in message[message.index(marker) + len(marker) :]:
        if not char.isdigit():
            break
        digits += char
    return int(digits) / 1000 if digits else None


class Result:
    """The outcome of a query: the rows (or object) and, when asked for, the count."""

    def __init__(self, data, count=None, status=200):
        """Store the data, the count and the HTTP status."""
        self.data = data
        self.count = count
        self.status = status


def quote(text, safe=""):
    """Percent-encode text for a URL, leaving unreserved characters and safe as is."""
    keep = _UNRESERVED + safe.encode()
    out = []
    for byte in str(text).encode():
        if byte in keep:
            out.append(chr(byte))
        else:
            out.append("%%%02X" % byte)
    return "".join(out)


def _value(value):
    """Format a filter value the way PostgREST expects."""
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    return str(value)


def _list_value(value):
    """Format one item of an in.(...) list, quoting it when it has reserved characters."""
    text = _value(value)
    for char in ',()"':
        if char in text:
            return '"' + text.replace('"', '\\"') + '"'
    return text


async def fetch(method, url, headers, body=None, body_base64=False, want_base64=False):
    """Call the shim's fetch and return status, headers and body as a dict.

    Raises RateLimited when Supabase refuses a call between functions, and
    SupabaseError (status 0) when the request couldn't be made at all.
    """
    raw = await get_host().fetch(
        url, method, json.dumps(headers), body, body_base64, want_base64
    )
    response = json.loads(raw)
    error = response.get("error")
    if error:
        message = error.get("message") or ""
        if error.get("name") == "RateLimitError":
            raise RateLimited(message, _retry_after(message))
        raise SupabaseError(0, (error.get("name") or "Error") + ": " + message)
    return response


def _error(response):
    """Build a SupabaseError from an error response."""
    message = response["body"]
    details = None
    try:
        details = json.loads(response["body"])
        if isinstance(details, dict):
            message = details.get("message") or details.get("error") or message
    except ValueError:
        pass
    return SupabaseError(response["status"], message, details)


def _decode(response):
    """Parse a JSON body, or return the text when it isn't JSON."""
    if not response["body"]:
        return None
    if "json" in response["headers"].get("content-type", ""):
        return json.loads(response["body"])
    return response["body"]


class Supabase:
    """A client for one project, with a key and the bearer token to send."""

    def __init__(self, url, key, token=None):
        """Store the project URL, the API key and the token for Authorization."""
        self.url = url.rstrip("/")
        self.key = key
        self.token = token or key
        self.functions = Functions(self)
        self.storage = Storage(self)

    def headers(self, extra=None):
        """The apikey and Authorization headers, plus any extra ones."""
        headers = {"apikey": self.key, "authorization": "Bearer " + self.token}
        if extra:
            headers.update(extra)
        return headers

    def table(self, name):
        """Start a query on a table or view."""
        return Query(self, name)

    from_ = table

    async def rpc(self, fn, params=None):
        """Call a Postgres function through PostgREST."""
        response = await fetch(
            "POST",
            self.url + "/rest/v1/rpc/" + quote(fn),
            self.headers({"content-type": "application/json"}),
            json.dumps(params or {}),
        )
        if response["status"] >= 400:
            raise _error(response)
        return Result(_decode(response), status=response["status"])


class Query:
    """A PostgREST query, built step by step and sent by execute()."""

    def __init__(self, client, table):
        """Start a plain select on the table."""
        self.client = client
        self.table = table
        self.method = "GET"
        self.params = []
        self.prefer = []
        self.body = None
        self.columns = None
        self.single_row = False

    def select(self, columns="*", count=None):
        """Choose the columns to return; count may be "exact", "planned" or "estimated"."""
        self.columns = columns
        if count:
            self.prefer.append("count=" + count)
        return self

    def _write(self, method, body, prefer):
        """Turn the query into a write that returns the affected rows."""
        self.method = method
        self.body = None if body is None else json.dumps(body)
        self.prefer.append("return=representation")
        self.prefer.extend(prefer)
        return self

    def insert(self, rows):
        """Insert one row (a dict) or several (a list of dicts)."""
        return self._write("POST", rows, [])

    def upsert(self, rows, on_conflict=None, ignore_duplicates=False):
        """Insert rows, updating (or skipping) the ones that conflict."""
        if on_conflict:
            self.params.append(("on_conflict", on_conflict))
        resolution = "ignore-duplicates" if ignore_duplicates else "merge-duplicates"
        return self._write("POST", rows, ["resolution=" + resolution])

    def update(self, values):
        """Update the rows the filters match."""
        return self._write("PATCH", values, [])

    def delete(self):
        """Delete the rows the filters match."""
        return self._write("DELETE", None, [])

    def filter(self, column, operator, value):
        """Add a raw PostgREST filter such as ("age", "gte", 18)."""
        self.params.append((column, operator + "." + _value(value)))
        return self

    def eq(self, column, value):
        """Keep rows where column equals value."""
        return self.filter(column, "eq", value)

    def neq(self, column, value):
        """Keep rows where column differs from value."""
        return self.filter(column, "neq", value)

    def gt(self, column, value):
        """Keep rows where column is greater than value."""
        return self.filter(column, "gt", value)

    def gte(self, column, value):
        """Keep rows where column is at least value."""
        return self.filter(column, "gte", value)

    def lt(self, column, value):
        """Keep rows where column is less than value."""
        return self.filter(column, "lt", value)

    def lte(self, column, value):
        """Keep rows where column is at most value."""
        return self.filter(column, "lte", value)

    def like(self, column, pattern):
        """Keep rows where column matches a LIKE pattern."""
        return self.filter(column, "like", pattern)

    def ilike(self, column, pattern):
        """Keep rows where column matches a case-insensitive LIKE pattern."""
        return self.filter(column, "ilike", pattern)

    def is_(self, column, value):
        """Keep rows where column IS value: None, True or False."""
        return self.filter(column, "is", value)

    def in_(self, column, values):
        """Keep rows where column is one of values."""
        items = ",".join(_list_value(v) for v in values)
        self.params.append((column, "in.(" + items + ")"))
        return self

    def order(self, column, desc=False, nullsfirst=None):
        """Sort by column; call again to add more sort keys."""
        term = column + (".desc" if desc else ".asc")
        if nullsfirst is not None:
            term += ".nullsfirst" if nullsfirst else ".nullslast"
        self.params.append(("order", term))
        return self

    def limit(self, count):
        """Return at most count rows."""
        self.params.append(("limit", str(count)))
        return self

    def offset(self, count):
        """Skip the first count rows."""
        self.params.append(("offset", str(count)))
        return self

    def single(self):
        """Return one object instead of a list; PostgREST errors unless exactly one row matches."""
        self.single_row = True
        return self

    def url(self):
        """The request URL with the select and every filter encoded."""
        params = list(self.params)
        if self.columns is not None:
            params.insert(0, ("select", self.columns))
        query = "&".join(quote(k) + "=" + quote(v, safe=",.()*:") for k, v in params)
        url = self.client.url + "/rest/v1/" + quote(self.table)
        return url + ("?" + query if query else "")

    async def execute(self):
        """Send the query and return a Result, raising SupabaseError on failure."""
        extra = {}
        if self.body is not None:
            extra["content-type"] = "application/json"
        if self.prefer:
            extra["prefer"] = ",".join(self.prefer)
        if self.single_row:
            extra["accept"] = "application/vnd.pgrst.object+json"
        response = await fetch(
            self.method, self.url(), self.client.headers(extra), self.body
        )
        if response["status"] >= 400:
            raise _error(response)
        count = None
        content_range = response["headers"].get("content-range", "")
        if "/" in content_range and not content_range.endswith("*"):
            count = int(content_range.split("/")[1])
        return Result(_decode(response), count, response["status"])


class Functions:
    """Call other Edge Functions in the same project."""

    def __init__(self, client):
        """Keep the client whose URL and token to use."""
        self.client = client

    async def invoke(self, name, body=None, headers=None, method="POST"):
        """Call a function with a JSON body and return its parsed response."""
        extra = dict(headers or {})
        payload = None
        if body is not None:
            payload = body if isinstance(body, str) else json.dumps(body)
            extra.setdefault("content-type", "application/json")
        response = await fetch(
            method,
            self.client.url + "/functions/v1/" + quote(name, safe="/"),
            self.client.headers(extra),
            payload,
        )
        if response["status"] >= 400:
            raise _error(response)
        return _decode(response)


class Storage:
    """Supabase Storage: buckets of files."""

    def __init__(self, client):
        """Keep the client whose URL and token to use."""
        self.client = client

    def from_(self, bucket):
        """Work with the files in one bucket."""
        return Bucket(self.client, bucket)


class Bucket:
    """Download, upload and remove files in a bucket."""

    def __init__(self, client, bucket):
        """Keep the client and the bucket name."""
        self.client = client
        self.bucket = bucket

    def _url(self, path):
        """The object URL for a path in this bucket."""
        return (
            self.client.url
            + "/storage/v1/object/"
            + quote(self.bucket)
            + "/"
            + quote(path.lstrip("/"), safe="/")
        )

    async def download(self, path):
        """Return a file's contents as bytes."""
        import binascii

        response = await fetch(
            "GET", self._url(path), self.client.headers(), want_base64=True
        )
        if response["status"] >= 400:
            raise _error(response)
        return binascii.a2b_base64(response["body"].encode())  # MicroPython wants bytes

    async def upload(
        self, path, data, content_type="application/octet-stream", upsert=False
    ):
        """Upload bytes or text to a path; upsert replaces an existing file."""
        import binascii

        if isinstance(data, str):
            data = data.encode()
        extra = {
            "content-type": content_type,
            "x-upsert": "true" if upsert else "false",
        }
        body = binascii.b2a_base64(bytes(data)).decode().strip()
        response = await fetch(
            "POST", self._url(path), self.client.headers(extra), body, body_base64=True
        )
        if response["status"] >= 400:
            raise _error(response)
        return _decode(response)

    async def remove(self, paths):
        """Delete files by path."""
        response = await fetch(
            "DELETE",
            self.client.url + "/storage/v1/object/" + quote(self.bucket),
            self.client.headers({"content-type": "application/json"}),
            json.dumps({"prefixes": list(paths)}),
        )
        if response["status"] >= 400:
            raise _error(response)
        return _decode(response)
