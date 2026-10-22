"""PyPI packages on Pyodide: pydantic validation and an openpyxl workbook."""

import base64
import io

import openpyxl
from pydantic import BaseModel, ValidationError, field_validator

from supie import App, Request

app = App()


class Lead(BaseModel):
    """One row of an upload."""

    email: str
    amount: float

    @field_validator("email")
    @classmethod
    def has_at(cls, value):
        """Emails need an @, and are stored in lower case."""
        if "@" not in value:
            raise ValueError("not an email")
        return value.lower()


def check(rows):
    """Validate rows, splitting them into accepted leads and errors."""
    ok, errors = [], []
    for i, row in enumerate(rows):
        try:
            ok.append(Lead(**row).model_dump())
        except ValidationError as error:
            errors.append({"row": i, "error": error.errors()[0]["msg"]})
    return {"ok": ok, "errors": errors}


@app.post("/validate")
def validate(req: Request):
    """Validate JSON rows."""
    return check(req.json()["rows"])


@app.post("/xlsx")
def xlsx(req: Request):
    """Validate the rows of an uploaded workbook (base64 in JSON)."""
    data = base64.b64decode(req.json()["workbook"])
    sheet = openpyxl.load_workbook(io.BytesIO(data), read_only=True).active
    rows = list(sheet.iter_rows(values_only=True))
    header = [str(h) for h in rows[0]]
    return check([dict(zip(header, row)) for row in rows[1:]])
