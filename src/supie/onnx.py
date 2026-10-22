"""ONNX models, run by the Edge Runtime's built-in ONNX engine.

    from supie.onnx import Model

    @app.on_boot
    async def setup():
        app.state.pd = await Model.from_storage("models", "pd.onnx")

    @app.post("/score")
    async def score(req):
        out = await app.state.pd.run({"features": [features(req.json())]})
        return {"pd": out["probabilities"][0][1]}

The shim downloads the model and runs it natively, outside WebAssembly, so a
gradient-boosted model scores a row in well under a millisecond. Python passes
plain nested lists in and gets plain nested lists back. Under CPython, as in
tests, models run with the onnxruntime package.

Like supie.app, this module runs under CPython, MicroPython and Pyodide.
"""

import json

from supie.app import env, get_host


class Model:
    """A loaded ONNX model: its input and output names, and run()."""

    def __init__(self, handle, inputs, outputs):
        """Wrap the host's handle for a loaded model."""
        self.handle = handle
        self.inputs = inputs
        self.outputs = outputs

    @classmethod
    def _from_info(cls, raw):
        """Build a Model from the host's JSON description of a session."""
        info = json.loads(raw)
        return cls(info["handle"], info["inputs"], info["outputs"])

    @classmethod
    async def from_url(cls, url, headers=None):
        """Download a model from a URL and load it."""
        raw = await get_host().onnx_load(url, json.dumps(headers or {}))
        return cls._from_info(raw)

    @classmethod
    async def from_storage(cls, bucket, path, client=None):
        """Load a model from Supabase Storage, as the service role unless a client is given."""
        if client is None:
            from supie.supabase import Supabase

            key = env["SUPABASE_SERVICE_ROLE_KEY"]
            client = Supabase(env["SUPABASE_URL"], key, key)
        bucket_files = client.storage.from_(bucket)
        return await cls.from_url(bucket_files._url(path), client.headers())

    async def run(self, inputs):
        """Run the model and return its outputs as nested lists, by name.

        inputs maps each input name to nested lists of float32 values, or to
        {"data": nested lists, "type": "int64"} for another element type.
        """
        payload = {}
        for name, value in inputs.items():
            if isinstance(value, dict):
                payload[name] = value
            else:
                payload[name] = {"data": value, "type": "float32"}
        raw = await get_host().onnx_run(self.handle, json.dumps(payload))
        return json.loads(raw)
