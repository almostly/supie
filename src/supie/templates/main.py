"""The __NAME__ function."""

from supie import App, HTTPError, Request

app = App()


@app.get("/")
def hello(req: Request):
    """Say hello, by name when one is given."""
    return {"hello": req.query.get("name", "world")}


@app.post("/echo")
def echo(req: Request):
    """Return the JSON body, which must be an object."""
    body = req.json()
    if not isinstance(body, dict):
        raise HTTPError(422, "expected a JSON object")
    return {"you_sent": body}
