"""The Python runtimes a function can use, fetched from npm and cached.

Each runtime is pinned to a version and checked against npm's sha512 integrity,
so a build never runs unverified code.
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import shutil
import tarfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Runtime:
    """An npm package holding a Python runtime, and the files a function needs."""

    name: str
    package: str
    version: str
    integrity: str
    files: tuple[str, ...]

    @property
    def cdn(self) -> str:
        """Pyodide's CDN folder for this version, where its prebuilt packages live."""
        return f"https://cdn.jsdelivr.net/pyodide/v{self.version}/full/"

    @property
    def tarball(self) -> str:
        """The package's tarball URL on the npm registry."""
        base = self.package.rsplit("/", 1)[-1]
        return f"https://registry.npmjs.org/{self.package}/-/{base}-{self.version}.tgz"


RUNTIMES = {
    "micropython": Runtime(
        name="micropython",
        package="@micropython/micropython-webassembly-pyscript",
        version="1.29.0-6",
        integrity="sha512-kLwGPJFsLY/2aUPmzZxpvauFSmKc0SlJWVvvEttULR0poVQ6W9HV//1fUJyxrGWAxCVDw9aYqJ0LAog0ZLjYVA==",
        files=("micropython.mjs", "micropython.wasm", "package.json"),
    ),
    "pyodide": Runtime(
        name="pyodide",
        package="pyodide",
        version="314.0.7",
        integrity="sha512-0YvXxEhfEdpLfb/XkM2BFAeMROq0iMUX2bzzH9pOttyMcWkwq+HbE5uyuGD82LN7y2q+SNvi/6V5JEsOlD2R1A==",
        files=(
            "package.json",
            "pyodide-lock.json",
            "pyodide.asm.mjs",
            "pyodide.asm.wasm",
            "pyodide.mjs",
            "python_stdlib.zip",
        ),
    ),
}


class FetchError(Exception):
    """A runtime couldn't be fetched or failed its integrity check."""


def cache_dir() -> Path:
    """Where supie keeps downloaded runtimes and built snapshots."""
    if os.environ.get("SUPIE_CACHE"):
        return Path(os.environ["SUPIE_CACHE"])
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "supie"


def verify(data: bytes, integrity: str) -> bool:
    """Check bytes against an npm integrity string such as sha512-<base64>."""
    algorithm, _, expected = integrity.partition("-")
    digest = hashlib.new(algorithm, data).digest()
    return base64.b64encode(digest).decode() == expected


def fetch(runtime: Runtime) -> Path:
    """Return a directory with the runtime's files, downloading them once."""
    target = cache_dir() / "runtimes" / f"{runtime.name}-{runtime.version}"
    if all((target / name).is_file() for name in runtime.files):
        return target
    try:
        with urllib.request.urlopen(runtime.tarball, timeout=120) as response:
            data = response.read()
    except OSError as error:
        raise FetchError(f"couldn't download {runtime.tarball}: {error}") from error
    if not verify(data, runtime.integrity):
        raise FetchError(
            f"{runtime.package}@{runtime.version} failed its integrity check"
        )
    partial = target.with_name(target.name + ".partial")
    shutil.rmtree(partial, ignore_errors=True)
    partial.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for name in runtime.files:
            member = archive.extractfile(f"package/{name}")
            if member is None:
                raise FetchError(f"{runtime.package} has no {name}")
            (partial / name).write_bytes(member.read())
    shutil.rmtree(target, ignore_errors=True)
    partial.rename(target)
    return target
