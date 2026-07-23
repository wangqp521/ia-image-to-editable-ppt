#!/usr/bin/env python3
"""Check the local rendering runtime once and save a traceable JSON report."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_executable(requested: str) -> Path | None:
    candidate = Path(requested).expanduser()
    if candidate.parent != Path(".") or candidate.is_absolute():
        resolved = candidate.resolve()
        return resolved if resolved.is_file() and os.access(resolved, os.X_OK) else None
    found = shutil.which(requested)
    return Path(found).resolve() if found else None


def _version(path: Path) -> str:
    last_result = "unavailable"
    for flag in ("--version", "-v"):
        try:
            completed = subprocess.run(
                [str(path), flag],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            last_result = f"unavailable: {exc}"
            continue
        output = (completed.stdout or completed.stderr).strip()
        if completed.returncode == 0 and output:
            return output.splitlines()[0]
        last_result = output.splitlines()[0] if output else f"exit={completed.returncode}"
    return last_result


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def inspect_runtime(args: argparse.Namespace) -> dict[str, Any]:
    errors: list[dict[str, str]] = []
    executables: dict[str, dict[str, Any]] = {}
    for name in ("soffice", "pdftoppm", "pdffonts"):
        requested = getattr(args, name)
        resolved = _resolve_executable(requested)
        executables[name] = {
            "requested": requested,
            "available": resolved is not None,
            "path": str(resolved) if resolved else None,
            "version": _version(resolved) if resolved else None,
        }
        if resolved is None:
            errors.append(
                {
                    "code": f"RUNTIME_EXECUTABLE_MISSING:{name}",
                    "detail": requested,
                }
            )

    fontconfig = args.fontconfig.expanduser().resolve()
    fontconfig_entry = {
        "path": str(fontconfig),
        "available": fontconfig.is_file(),
        "sha256": _sha256(fontconfig) if fontconfig.is_file() else None,
    }
    if not fontconfig.is_file():
        errors.append(
            {
                "code": "RUNTIME_FONTCONFIG_MISSING",
                "detail": str(fontconfig),
            }
        )

    modules: dict[str, dict[str, Any]] = {}
    for name in args.python_module:
        available = importlib.util.find_spec(name) is not None
        version = None
        if available:
            try:
                version = importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                version = "stdlib-or-unversioned"
        modules[name] = {"available": available, "version": version}
        if not available:
            errors.append(
                {
                    "code": f"RUNTIME_PYTHON_MODULE_MISSING:{name}",
                    "detail": name,
                }
            )

    return {
        "valid": not errors,
        "errors": errors,
        "python": {
            "executable": str(Path(sys.executable).resolve()),
            "version": sys.version.split()[0],
        },
        "executables": executables,
        "fontconfig": fontconfig_entry,
        "python_modules": modules,
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--soffice", required=True)
    parser.add_argument("--pdftoppm", required=True)
    parser.add_argument("--pdffonts", required=True)
    parser.add_argument("--fontconfig", type=Path, required=True)
    parser.add_argument("--python-module", action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    result = inspect_runtime(args)
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    _atomic_write(args.output, text)
    print(text, end="")
    return 0 if result["valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
