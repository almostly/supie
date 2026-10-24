# Models

Supabase's Edge Runtime has a native ONNX engine built in. supie gives Python handlers access to it, so a model scores outside WebAssembly, at native speed:

```python
from supie import App, Request
from supie.onnx import Model

app = App()
FEATURES = ["age", "income", "dti", "utilization", "delinquencies"]


@app.on_boot
async def setup():
    app.state.model = await Model.from_storage("models", "pd/v1.onnx")  # a private bucket


@app.post("/")
async def score(req: Request):
    applicant = req.json()
    out = await app.state.model.run({"features": [[applicant[f] for f in FEATURES]]})
    return {"pd": out["probabilities"][0][1]}
```

`Model.from_storage` downloads the model as the service role, unless you pass a client. `Model.from_url(url, headers)` loads one from anywhere else. `run` takes nested lists of float32, or `{"data": ..., "type": "int64"}` for other types, and returns nested lists by output name. Under CPython, as in tests, models run with the `onnxruntime` package, and the test client's `fetch` serves the model file.

To get a model into Storage, publish it. The source can be an ONNX file, an MLflow model folder, or an MLflow URI, so a registry alias works:

```bash
pip install 'supie[models]' mlflow-skinny
supie model publish models:/pd@champion --bucket models --path pd/v3.onnx --project-ref <ref>
```

`publish` downloads the model with MLflow and takes its ONNX flavor. It removes any ZipMap, the output classifier exports from CatBoost, scikit-learn and others end in, which the engine can't return. Then it uploads to the bucket, creating it as private if needed. The service role key comes from the Supabase CLI with `--project-ref`, or from `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY`. MLflow runs only here, at build time; the function never imports it. Log an ONNX copy next to the native model, for example with `mlflow.onnx.log_model`, and compare their predictions on a holdout set before promoting a new champion.

`supie model prepare in.onnx out.onnx` does only the ZipMap step, locally.

A CatBoost model of 1.6 MB, published from an MLflow registry alias to a private bucket on hosted Supabase:

| | |
|---|---|
| 500 holdout rows | 1.8 s end to end, largest difference from native CatBoost 2.4e-7 |
| one applicant, 20 requests | median 0.53 s end to end, each on a fresh worker |

On a fresh worker, about 0.3 s of that is downloading the model from Storage. A gradient-boosted model scores a row in about 0.1 ms.
