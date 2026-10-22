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


def resolve(source: str, workdir: Path) -> Path:
    """The ONNX file behind a path, an MLflow model folder, or an MLflow model URI.

    URIs such as models:/pd@champion or runs:/<id>/model are downloaded with
    MLflow into workdir; their MLmodel file names the ONNX flavor's file.
    """
    path = Path(source)
    if ":/" in source and not path.exists():
        try:
            import mlflow
        except ImportError as error:
            raise RuntimeError(
                "MLflow model URIs need MLflow: pip install mlflow-skinny"
            ) from error
        path = Path(mlflow.artifacts.download_artifacts(source, dst_path=str(workdir)))
    if path.is_dir():
        mlmodel = path / "MLmodel"
        if not mlmodel.is_file():
            raise RuntimeError(f"{path} has no MLmodel file")
        import yaml

        flavors = yaml.safe_load(mlmodel.read_text()).get("flavors", {})
        if "onnx" not in flavors:
            raise RuntimeError(
                f"{source} has no ONNX flavor (it has {', '.join(flavors)}); "
                "log an ONNX copy with mlflow.onnx.log_model"
            )
        path = path / flavors["onnx"]["data"]
    if not path.is_file():
        raise RuntimeError(f"no ONNX model at {source}")
    return path


def _request(
    method: str, url: str, headers: dict[str, str], body: bytes | None = None
) -> tuple[int, str]:
    """One HTTP request, returning status and body without raising on HTTP errors."""
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


def upload(model: Path, bucket: str, object_path: str, url: str, key: str) -> str:
    """Upload a model to Storage as the service role, creating a private bucket if needed."""
    import json

    base = url.rstrip("/") + "/storage/v1"
    auth = {"apikey": key, "authorization": f"Bearer {key}"}
    status, _ = _request("GET", f"{base}/bucket/{bucket}", auth)
    if status in (400, 404):  # Storage answers 400 for a bucket that doesn't exist
        bucket_json = json.dumps({"id": bucket, "name": bucket, "public": False})
        status, text = _request(
            "POST",
            f"{base}/bucket",
            {**auth, "content-type": "application/json"},
            bucket_json.encode(),
        )
        if status >= 400:
            raise RuntimeError(f"creating bucket {bucket}: HTTP {status} {text}")
    target = f"{bucket}/{object_path.lstrip('/')}"
    status, text = _request(
        "POST",
        f"{base}/object/{target}",
        {**auth, "content-type": "application/octet-stream", "x-upsert": "true"},
        model.read_bytes(),
    )
    if status >= 400:
        raise RuntimeError(f"uploading to {target}: HTTP {status} {text}")
    return target
