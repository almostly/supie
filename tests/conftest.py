"""Shared fixtures: a throwaway Supabase project, and the Docker images to test against."""

import pytest
from images import IMAGES


CONFIG = """# a comment supie must keep
project_id = "demo"

[functions.existing]
verify_jwt = false
"""


@pytest.fixture
def project(tmp_path):
    """A folder with supabase/config.toml, as `supabase init` leaves it."""
    (tmp_path / "supabase").mkdir()
    (tmp_path / "supabase" / "config.toml").write_text(CONFIG)
    return tmp_path


@pytest.fixture(scope="session")
def images():
    """The Docker images for the edge and postgres tests."""
    return IMAGES


def tiny_model():
    """A two-feature logistic model as ONNX bytes: p = sigmoid(2*x0 - 3*x1 + 0.5)."""
    onnx = pytest.importorskip("onnx")
    from onnx import TensorProto, helper

    weights = helper.make_tensor("w", TensorProto.FLOAT, [2, 1], [2.0, -3.0])
    bias = helper.make_tensor("b", TensorProto.FLOAT, [1], [0.5])
    graph = helper.make_graph(
        [
            helper.make_node("MatMul", ["features", "w"], ["z0"]),
            helper.make_node("Add", ["z0", "b"], ["z"]),
            helper.make_node("Sigmoid", ["z"], ["probability"]),
        ],
        "tiny",
        [helper.make_tensor_value_info("features", TensorProto.FLOAT, ["N", 2])],
        [helper.make_tensor_value_info("probability", TensorProto.FLOAT, ["N", 1])],
        [weights, bias],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    model.ir_version = 8
    onnx.checker.check_model(model)
    return model.SerializeToString()


@pytest.fixture
def model_bytes():
    """The tiny logistic model's ONNX bytes."""
    return tiny_model()
