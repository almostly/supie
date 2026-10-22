"""Prepare ONNX models for the Edge Runtime's ONNX engine. Runs under CPython only.

Classifier exports from CatBoost, scikit-learn and others end in a ZipMap node,
whose output is a list of {label: probability} maps rather than a tensor. The
engine can only return tensors, so prepare() removes the ZipMap and exposes the
probability tensor it wraps under the same output name.
"""

from __future__ import annotations

from pathlib import Path


def prepare(source: Path, target: Path) -> list[str]:
    """Write a copy of the model without ZipMap outputs; return what was changed."""
    try:
        import onnx
        from onnx import TensorProto, helper
    except ImportError as error:
        raise RuntimeError(
            "preparing models needs onnx: pip install 'supie[models]'"
        ) from error

    model = onnx.load(str(source))
    graph = model.graph
    changes = []
    for zipmap in [node for node in graph.node if node.op_type == "ZipMap"]:
        wrapped, name = zipmap.input[0], zipmap.output[0]
        graph.node.remove(zipmap)
        for node in graph.node:
            node.output[:] = [name if out == wrapped else out for out in node.output]
        old = next(o for o in graph.output if o.name == name)
        graph.output.remove(old)
        graph.output.append(
            helper.make_tensor_value_info(name, TensorProto.FLOAT, ["N", None])
        )
        changes.append(f"{name}: ZipMap replaced by the probability tensor")
    onnx.checker.check_model(model)
    onnx.save(model, str(target))
    return changes
