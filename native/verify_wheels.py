"""Fail a release if its wheel is missing generated UniFFI Python bindings.

Cross-compiled wheels cannot be imported on the x86_64 release runner, but the
archive contents can still be checked before either PyPI or GitHub receives
them. This specifically guards against a native-only wheel whose empty package
initializer causes `import filen_sdk_native` to succeed without exposing
`Credentials` or `SdkClient`.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path


REQUIRED_BINDINGS = (
    "class Credentials",
    "class SdkClient",
    "async def login(",
)


def verify(path: Path) -> None:
    if path.suffix != ".whl":
        return
    with zipfile.ZipFile(path) as wheel:
        names = set(wheel.namelist())
        package = next(
            (
                name.rsplit("/", 1)[0]
                for name in names
                if name.endswith("/libfilen_sdk_native.so")
            ),
            None,
        )
        if package is None:
            raise SystemExit(f"{path.name}: wheel metadata is missing")
        init_name = f"{package}/__init__.py"
        binding_name = f"{package}/filen_sdk_native.py"
        library_name = f"{package}/libfilen_sdk_native.so"
        missing = [
            name for name in (init_name, binding_name, library_name) if name not in names
        ]
        if missing:
            raise SystemExit(f"{path.name}: missing wheel files: {', '.join(missing)}")
        init_source = wheel.read(init_name).decode()
        binding_source = wheel.read(binding_name).decode()
        if "from .filen_sdk_native import *" not in init_source:
            raise SystemExit(f"{path.name}: package initializer does not expose UniFFI bindings")
        for symbol in REQUIRED_BINDINGS:
            if symbol not in binding_source:
                raise SystemExit(f"{path.name}: generated bindings omit {symbol!r}")
    print(f"verified {path.name}")


def main(args: list[str]) -> None:
    paths = [Path(arg) for arg in args]
    wheels = [path for path in paths if path.suffix == ".whl"]
    if not wheels:
        raise SystemExit("no wheels supplied")
    for path in wheels:
        verify(path)


if __name__ == "__main__":
    main(sys.argv[1:])
