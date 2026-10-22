"""supie.onnx under CPython, with onnxruntime standing in for the Edge Runtime's engine."""

import math

import pytest

from supie import App
from supie.onnx import Model
from supie.testing import FakeResponse, TestClient

pytest.importorskip("onnxruntime")


def expected(x0, x1):
    """The tiny model's probability, computed directly."""
    return 1 / (1 + math.exp(-(2 * x0 - 3 * x1 + 0.5)))


def scoring_app():
    """An app that loads the model from Storage at boot and scores rows."""
    app = App()

    @app.on_boot
    async def setup():
        """Load the model once per worker."""
        app.state.model = await Model.from_storage("models", "v1/tiny.onnx")

    @app.post("/score")
    async def score(req):
        """Score every row in the body."""
        out = await app.state.model.run({"features": req.json()["rows"]})
        return {"pd": [row[0] for row in out["probability"]]}

    return app


def test_load_from_storage_and_score(model_bytes):
    """The model comes from Storage as the service role and scores like the formula."""

    def fetch(call):
        """Serve the model file."""
        assert (
            call.url == "https://test.supabase.co/storage/v1/object/models/v1/tiny.onnx"
        )
        assert call.headers["authorization"] == "Bearer service-role-key"
        return FakeResponse(model_bytes)

    client = TestClient(scoring_app(), fetch=fetch)
    rows = [[0.0, 0.0], [1.0, 0.5], [-1.0, 2.0]]
    pd = client.post("/score", json={"rows": rows}).json()["pd"]
    assert pd == pytest.approx([expected(*row) for row in rows], abs=1e-6)
    assert client.app.state.model.inputs == ["features"]
    assert client.app.state.model.outputs == ["probability"]
    client.post("/score", json={"rows": rows})
    assert len(client.calls) == 1  # loaded once, at boot


def test_missing_model_fails_boot():
    """A model that isn't in Storage fails boot with a clear error."""
    client = TestClient(
        scoring_app(), fetch=lambda call: FakeResponse("", 404), raise_errors=False
    )
    response = client.post("/score", json={"rows": [[0, 0]]})
    assert response.status == 500
    assert "HTTP 404" in response.log


def test_model_prepare_removes_zipmap(tmp_path, model_bytes):
    """A classifier export ending in ZipMap gets a plain probability tensor instead."""
    import onnx
    import onnxruntime
    from onnx import TensorProto, helper
    from typer.testing import CliRunner

    from supie.cli import app

    model = onnx.load_from_string(model_bytes)
    graph = model.graph
    graph.node.append(
        helper.make_node("Concat", ["probability", "probability"], ["pair"], axis=1)
    )
    graph.node.append(
        helper.make_node(
            "ZipMap",
            ["pair"],
            ["probabilities"],
            domain="ai.onnx.ml",
            classlabels_int64s=[0, 1],
        )
    )
    graph.output.append(
        helper.make_value_info(
            "probabilities",
            helper.make_sequence_type_proto(
                helper.make_map_type_proto(
                    TensorProto.INT64,
                    helper.make_tensor_type_proto(TensorProto.FLOAT, []),
                )
            ),
        )
    )
    model.opset_import.append(helper.make_opsetid("ai.onnx.ml", 3))
    source, target = tmp_path / "zipmap.onnx", tmp_path / "plain.onnx"
    onnx.save(model, str(source))

    result = CliRunner().invoke(app, ["model", "prepare", str(source), str(target)])
    assert result.exit_code == 0, result.output
    assert "ZipMap replaced" in result.output
    session = onnxruntime.InferenceSession(str(target))
    out = dict(
        zip(
            [o.name for o in session.get_outputs()],
            session.run(None, {"features": [[0.0, 0.0]]}),
        )
    )
    assert out["probabilities"].shape == (1, 2)
    assert out["probabilities"][0][0] == pytest.approx(expected(0, 0), abs=1e-6)
