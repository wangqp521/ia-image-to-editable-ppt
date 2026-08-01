#!/usr/bin/env python3
"""Check the local rendering runtime once and save a traceable JSON report."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


PRERELEASE_RENDERER_PATTERN = re.compile(
    r"(?:libreofficedev|\b(?:alpha|beta|rc)\d*\b)",
    re.IGNORECASE,
)
PREVIEW_SIZE = [1920, 1080]
REQUIRED_EXECUTABLES = ("soffice", "pdftoppm", "pdffonts", "pdftotext")
_MACHO_MAGICS = frozenset(
    {
        bytes.fromhex(value)
        for value in (
            "feedface",
            "cefaedfe",
            "feedfacf",
            "cffaedfe",
            "cafebabe",
            "bebafeca",
            "cafebabf",
            "bfbafeca",
        )
    }
)
_SYSTEM_LIBRARY_PREFIXES = ("/System/", "/usr/lib/")
_MAX_DYNAMIC_LIBRARIES = 256


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _subprocess_env() -> dict[str, str]:
    env = {
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": os.defpath,
    }
    if os.name == "nt" and isinstance(os.environ.get("SYSTEMROOT"), str):
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    return env


def _is_macho(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            return stream.read(4) in _MACHO_MAGICS
    except OSError:
        return False


def _otool(path: Path, flag: str) -> list[str]:
    completed = subprocess.run(
        ["/usr/bin/otool", flag, str(path)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        env=_subprocess_env(),
        timeout=10,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"otool {flag} failed for {path}")
    return completed.stdout.splitlines()


def _macho_dependencies(path: Path) -> list[str]:
    return [
        line.strip().split(" (", 1)[0]
        for line in _otool(path, "-L")[1:]
        if line[:1].isspace() and " (" in line
    ]


def _macho_rpaths(path: Path) -> list[str]:
    lines = _otool(path, "-l")
    values: list[str] = []
    for index, line in enumerate(lines):
        if line.strip() != "cmd LC_RPATH":
            continue
        for candidate in lines[index + 1 : index + 6]:
            stripped = candidate.strip()
            if stripped.startswith("path ") and " (offset " in stripped:
                values.append(stripped[5:].split(" (offset ", 1)[0])
                break
    return values


def _expand_macho_token(value: str, *, loader: Path, executable: Path) -> Path:
    if value == "@loader_path":
        return loader.parent
    if value.startswith("@loader_path/"):
        return loader.parent / value[len("@loader_path/") :]
    if value == "@executable_path":
        return executable.parent
    if value.startswith("@executable_path/"):
        return executable.parent / value[len("@executable_path/") :]
    return Path(value)


def _resolve_macho_dependency(
    install_name: str,
    *,
    loader: Path,
    executable: Path,
    rpaths: list[str],
) -> Path | None:
    if install_name.startswith(_SYSTEM_LIBRARY_PREFIXES):
        return None
    candidates: list[Path]
    if install_name.startswith("@rpath/"):
        suffix = install_name[len("@rpath/") :]
        candidates = [
            _expand_macho_token(
                rpath,
                loader=loader,
                executable=executable,
            )
            / suffix
            for rpath in rpaths
        ]
    elif install_name.startswith(("@loader_path", "@executable_path")):
        candidates = [
            _expand_macho_token(
                install_name,
                loader=loader,
                executable=executable,
            )
        ]
    elif install_name.startswith("/"):
        candidates = [Path(install_name)]
    else:
        candidates = []
    for candidate in candidates:
        try:
            return candidate.resolve(strict=True)
        except OSError:
            continue
    raise RuntimeError(
        f"cannot resolve dynamic dependency {install_name!r} for {loader}"
    )


def _dynamic_libraries(executable: Path) -> list[dict[str, Any]]:
    if sys.platform != "darwin" or not _is_macho(executable):
        return []
    aliases_by_path: dict[Path, set[str]] = {}
    alias_owner: dict[str, Path] = {}
    seen: set[Path] = set()
    pending = [executable.resolve(strict=True)]
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        if len(seen) > _MAX_DYNAMIC_LIBRARIES + 1:
            raise RuntimeError("dynamic dependency closure exceeds its limit")
        rpaths = _macho_rpaths(current)
        for install_name in _macho_dependencies(current):
            resolved = _resolve_macho_dependency(
                install_name,
                loader=current,
                executable=executable,
                rpaths=rpaths,
            )
            if resolved is None:
                continue
            aliases = {
                Path(install_name).name,
                resolved.name,
            }
            for alias in aliases:
                owner = alias_owner.setdefault(alias, resolved)
                if owner != resolved:
                    raise RuntimeError(
                        f"dynamic library alias collision for {alias!r}"
                    )
            aliases_by_path.setdefault(resolved, set()).update(aliases)
            if resolved != current and resolved not in seen:
                pending.append(resolved)
    return [
        {
            "path": str(path),
            "sha256": _sha256(path),
            "load_names": sorted(aliases),
        }
        for path, aliases in sorted(
            aliases_by_path.items(), key=lambda item: str(item[0])
        )
    ]


def is_stable_libreoffice_version(version: str) -> bool:
    return bool(version.strip()) and PRERELEASE_RENDERER_PATTERN.search(version) is None


def _resolve_executable(requested: str) -> Path | None:
    candidate = Path(requested).expanduser()
    if candidate.parent != Path(".") or candidate.is_absolute():
        resolved = candidate.resolve()
        return resolved if resolved.is_file() and os.access(resolved, os.X_OK) else None
    found = shutil.which(requested)
    return Path(found).resolve() if found else None


def _version(path: Path) -> str | None:
    for flag in ("--version", "-v"):
        try:
            completed = subprocess.run(
                [str(path), flag],
                check=False,
                capture_output=True,
                text=True,
                env=_subprocess_env(),
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        output = (completed.stdout or completed.stderr).strip()
        if completed.returncode == 0 and output:
            return output.splitlines()[0]
    return None


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
    for name in REQUIRED_EXECUTABLES:
        requested = getattr(args, name)
        resolved = _resolve_executable(requested)
        dynamic_libraries: list[dict[str, Any]] = []
        if name == "pdffonts" and resolved is not None:
            try:
                dynamic_libraries = _dynamic_libraries(resolved)
            except (
                OSError,
                RuntimeError,
                UnicodeError,
                subprocess.SubprocessError,
            ) as exc:
                errors.append(
                    {
                        "code": "RUNTIME_DYNAMIC_LIBRARY_INSPECTION_FAILED:pdffonts",
                        "detail": str(exc),
                    }
                )
        executables[name] = {
            "requested": requested,
            "available": resolved is not None,
            "path": str(resolved) if resolved else None,
            "version": _version(resolved) if resolved else None,
            "sha256": _sha256(resolved) if resolved else None,
            "dynamic_libraries": dynamic_libraries,
        }
        if resolved is None:
            errors.append(
                {
                    "code": f"RUNTIME_EXECUTABLE_MISSING:{name}",
                    "detail": requested,
                }
            )
        elif executables[name]["version"] is None:
            errors.append(
                {
                    "code": f"RUNTIME_EXECUTABLE_VERSION_UNAVAILABLE:{name}",
                    "detail": str(resolved),
                }
            )

    soffice_version = executables["soffice"]["version"]
    if (
        executables["soffice"]["available"]
        and isinstance(soffice_version, str)
        and not is_stable_libreoffice_version(soffice_version)
    ):
        errors.append(
            {
                "code": "RUNTIME_RENDERER_PRERELEASE_FORBIDDEN",
                "detail": soffice_version,
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

    result = {
        "valid": False,
        "errors": errors,
        "renderer_backend": "libreoffice",
        "preview_size": PREVIEW_SIZE,
        "python": {
            "executable": str(Path(sys.executable).resolve()),
            "version": sys.version.split()[0],
        },
        "executables": executables,
        "fontconfig": fontconfig_entry,
        "python_modules": modules,
    }
    expected_runtime = getattr(args, "expected_runtime", None)
    if expected_runtime is not None:
        try:
            expected = json.loads(
                expected_runtime.expanduser().resolve().read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            errors.append(
                {
                    "code": "RUNTIME_RENDERER_IDENTITY_MISMATCH",
                    "detail": f"cannot read expected runtime: {exc}",
                }
            )
        else:
            invalid_containers: list[str] = []
            expected_executables: dict[str, Any] = {}
            expected_fontconfig: dict[str, Any] = {}
            if not isinstance(expected, dict):
                invalid_containers.append("expected-runtime")
            else:
                candidate_executables = expected.get("executables")
                candidate_fontconfig = expected.get("fontconfig")
                if not isinstance(candidate_executables, dict):
                    invalid_containers.append("executables")
                else:
                    expected_executables = candidate_executables
                    for name in REQUIRED_EXECUTABLES:
                        if not isinstance(expected_executables.get(name), dict):
                            invalid_containers.append(f"executables.{name}")
                if not isinstance(candidate_fontconfig, dict):
                    invalid_containers.append("fontconfig")
                else:
                    expected_fontconfig = candidate_fontconfig
            if invalid_containers:
                errors.append(
                    {
                        "code": "RUNTIME_RENDERER_IDENTITY_MISMATCH",
                        "detail": "invalid containers: "
                        + ", ".join(sorted(invalid_containers)),
                    }
                )
            else:
                keys = (
                    ("renderer_backend", result.get("renderer_backend"), expected.get("renderer_backend")),
                    ("preview_size", result.get("preview_size"), expected.get("preview_size")),
                    ("fontconfig.sha256", result["fontconfig"].get("sha256"), expected_fontconfig.get("sha256")),
                )
                mismatches = [
                    key for key, actual, wanted in keys if actual != wanted
                ]
                for name in REQUIRED_EXECUTABLES:
                    actual_tool = result["executables"].get(name, {})
                    expected_tool = expected_executables[name]
                    for field in (
                        "path",
                        "version",
                        "sha256",
                        "dynamic_libraries",
                    ):
                        if actual_tool.get(field) != expected_tool.get(field):
                            mismatches.append(f"executables.{name}.{field}")
                if mismatches:
                    errors.append(
                        {
                            "code": "RUNTIME_RENDERER_IDENTITY_MISMATCH",
                            "detail": ", ".join(sorted(mismatches)),
                        }
                    )
    result["valid"] = not errors
    return result


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--soffice", required=True)
    parser.add_argument("--pdftoppm", required=True)
    parser.add_argument("--pdffonts", required=True)
    parser.add_argument("--pdftotext", required=True)
    parser.add_argument("--fontconfig", type=Path, required=True)
    parser.add_argument("--expected-runtime", type=Path)
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
