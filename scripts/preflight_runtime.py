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

from lib.font_runtime import (
    REQUIRED_BOLD_FAMILIES,
    SUPPORTED_FAMILIES,
    font_runtime_identity,
    validate_font_runtime,
)


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
_WINDOWS_FONT_FILES = {
    "Microsoft YaHei": ("msyh.ttc", "msyhbd.ttc"),
    "STKaiti": ("STKAITI.TTF", None),
}
_FONTCONFIG_FAMILIES = SUPPORTED_FAMILIES


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


def _runtime_error(code: str, detail: str) -> ValueError:
    return ValueError(f"{code}: {detail}")


def _font_artifact(path: Path, face_index: int) -> dict[str, Any]:
    resolved = path.resolve()
    return {
        "path": str(resolved),
        "sha256": _sha256(resolved),
        "face_index": face_index,
    }


def resolve_windows_font_runtime(family: str, windows_dir: Path) -> dict[str, Any]:
    """Resolve fixed font files from one Windows system Fonts directory."""
    try:
        regular_name, bold_name = _WINDOWS_FONT_FILES[family]
    except KeyError as exc:
        raise _runtime_error(
            "RUNTIME_FIXED_FONT_UNSUPPORTED",
            f"unsupported family {family!r}",
        ) from exc

    fonts_dir = windows_dir.expanduser().resolve() / "Fonts"
    regular_path = fonts_dir / regular_name
    if not regular_path.is_file():
        raise _runtime_error(
            "RUNTIME_FIXED_FONT_REGULAR_MISSING",
            str(regular_path),
        )

    bold = None
    if bold_name is not None:
        bold_path = fonts_dir / bold_name
        if not bold_path.is_file():
            raise _runtime_error(
                "RUNTIME_FIXED_FONT_BOLD_MISSING",
                str(bold_path),
            )
        bold = _font_artifact(bold_path, 0)

    return validate_font_runtime(
        {
            "policy": "fixed",
            "family": family,
            "provider": "windows-system",
            "allow_substitution": False,
            "regular": _font_artifact(regular_path, 0),
            "bold": bold,
            "bold_available": bold is not None,
        }
    )


def _fontconfig_match(
    family: str,
    style: str,
    fontconfig: Path,
    fc_match: str,
) -> tuple[str, str, Path, int]:
    env = _subprocess_env()
    env["FONTCONFIG_FILE"] = str(fontconfig.expanduser().resolve())
    try:
        completed = subprocess.run(
            [
                fc_match,
                "-f",
                "%{family}\t%{style}\t%{file}\t%{index}\n",
                f"{family}:style={style}",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            env=env,
            timeout=10,
        )
    except (OSError, UnicodeError, subprocess.SubprocessError) as exc:
        raise _runtime_error("RUNTIME_FIXED_FONT_QUERY_FAILED", str(exc)) from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise _runtime_error("RUNTIME_FIXED_FONT_QUERY_FAILED", detail or family)
    line = completed.stdout.splitlines()[0] if completed.stdout.splitlines() else ""
    fields = line.split("\t")
    if len(fields) != 4 or not all(fields):
        raise _runtime_error(
            "RUNTIME_FIXED_FONT_QUERY_FAILED",
            f"unexpected fc-match output for {family} {style}",
        )
    try:
        face_index = int(fields[3].strip())
    except ValueError as exc:
        raise _runtime_error(
            "RUNTIME_FIXED_FONT_QUERY_FAILED",
            f"invalid face index for {family} {style}",
        ) from exc
    if face_index < 0:
        raise _runtime_error(
            "RUNTIME_FIXED_FONT_QUERY_FAILED",
            f"invalid face index for {family} {style}",
        )
    return fields[0].strip(), fields[1].strip(), Path(fields[2].strip()), face_index


def _family_is_exact(expected: str, resolved: str) -> bool:
    return any(name.strip() == expected for name in resolved.split(","))


def _style_tokens(style: str) -> set[str]:
    return {
        token.strip().casefold()
        for token in re.split(r"[,;]", style)
        if token.strip()
    }


def resolve_fontconfig_font_runtime(
    family: str,
    fontconfig: Path,
    fc_match: str = "fc-match",
) -> dict[str, Any]:
    """Resolve exact Regular/Bold files through fontconfig without aliases."""
    if family not in _FONTCONFIG_FAMILIES:
        raise _runtime_error(
            "RUNTIME_FIXED_FONT_UNSUPPORTED",
            f"unsupported family {family!r}",
        )

    regular_family, regular_style, regular_path, regular_face_index = _fontconfig_match(
        family,
        "Regular",
        fontconfig,
        fc_match,
    )
    if not _family_is_exact(family, regular_family):
        raise _runtime_error(
            "RUNTIME_FIXED_FONT_FAMILY_MISMATCH",
            f"requested {family!r}, resolved {regular_family!r}",
        )
    if not (_style_tokens(regular_style) & {"regular", "normal", "book"}):
        raise _runtime_error(
            "RUNTIME_FIXED_FONT_STYLE_MISMATCH",
            f"requested Regular, resolved {regular_style!r}",
        )
    if not regular_path.is_file():
        raise _runtime_error(
            "RUNTIME_FIXED_FONT_REGULAR_MISSING",
            str(regular_path),
        )

    bold = None
    bold_family, bold_style, bold_path, bold_face_index = _fontconfig_match(
        family,
        "Bold",
        fontconfig,
        fc_match,
    )
    bold_is_exact = (
        _family_is_exact(family, bold_family)
        and "bold" in _style_tokens(bold_style)
        and bold_path.is_file()
        and (bold_path.resolve(), bold_face_index)
        != (regular_path.resolve(), regular_face_index)
    )
    if bold_is_exact:
        bold = _font_artifact(bold_path, bold_face_index)
    elif family in REQUIRED_BOLD_FAMILIES:
        detail = f"requested {family!r} Bold, resolved {bold_family!r}/{bold_style!r}"
        raise _runtime_error("RUNTIME_FIXED_FONT_BOLD_MISSING", detail)

    return validate_font_runtime(
        {
            "policy": "fixed",
            "family": family,
            "provider": "fontconfig",
            "allow_substitution": False,
            "regular": _font_artifact(regular_path, regular_face_index),
            "bold": bold,
            "bold_available": bold is not None,
        }
    )


def _selected_provider(requested: str) -> str:
    if requested == "auto":
        if os.name == "nt":
            return "windows-system"
        if sys.platform == "darwin":
            return "fontconfig"
        raise _runtime_error(
            "RUNTIME_FONT_PROVIDER_UNSUPPORTED",
            f"automatic provider is unsupported on {sys.platform}",
        )
    if requested == "windows-system" and os.name != "nt":
        raise _runtime_error(
            "RUNTIME_FONT_PROVIDER_UNSUPPORTED",
            "windows-system requires Windows",
        )
    if requested == "fontconfig" and sys.platform != "darwin":
        raise _runtime_error(
            "RUNTIME_FONT_PROVIDER_UNSUPPORTED",
            "fontconfig provider requires macOS",
        )
    return requested


def default_font_family(provider: str) -> str:
    """Return the fixed default family for one supported platform provider."""
    if provider == "fontconfig":
        return "Hiragino Sans GB"
    if provider == "windows-system":
        return "Microsoft YaHei"
    raise _runtime_error(
        "RUNTIME_FONT_PROVIDER_UNSUPPORTED",
        f"unsupported provider {provider!r}",
    )


def _append_runtime_error(errors: list[dict[str, str]], exc: ValueError) -> None:
    code, separator, detail = str(exc).partition(": ")
    errors.append({"code": code, "detail": detail if separator else str(exc)})


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

    fontconfig_entry: dict[str, Any] | None = None
    font_runtime: dict[str, Any] | None = None
    try:
        provider = _selected_provider(args.font_provider)
    except ValueError as exc:
        _append_runtime_error(errors, exc)
        provider = None

    font_family = args.font_family
    if provider is not None and font_family is None:
        font_family = default_font_family(provider)

    if provider == "fontconfig":
        if args.fontconfig is None:
            errors.append(
                {
                    "code": "RUNTIME_FONTCONFIG_MISSING",
                    "detail": "--fontconfig is required for the fontconfig provider",
                }
            )
        else:
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
            else:
                fc_match = shutil.which("fc-match")
                if fc_match is None:
                    errors.append(
                        {
                            "code": "RUNTIME_EXECUTABLE_MISSING:fc-match",
                            "detail": "fc-match",
                        }
                    )
                else:
                    try:
                        font_runtime = resolve_fontconfig_font_runtime(
                            font_family,
                            fontconfig,
                            fc_match=str(Path(fc_match).resolve()),
                        )
                    except ValueError as exc:
                        _append_runtime_error(errors, exc)
    elif provider == "windows-system":
        windows_root = os.environ.get("WINDIR") or os.environ.get("SYSTEMROOT")
        if not windows_root:
            windows_root = r"C:\Windows"
        try:
            font_runtime = resolve_windows_font_runtime(
                font_family,
                Path(windows_root),
            )
        except ValueError as exc:
            _append_runtime_error(errors, exc)

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
        "font_runtime": font_runtime,
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
            expected_fontconfig: dict[str, Any] | None = None
            expected_font_runtime: dict[str, Any] | None = None
            if not isinstance(expected, dict):
                invalid_containers.append("expected-runtime")
            else:
                candidate_executables = expected.get("executables")
                candidate_fontconfig = expected.get("fontconfig")
                candidate_font_runtime = expected.get("font_runtime")
                if not isinstance(candidate_executables, dict):
                    invalid_containers.append("executables")
                else:
                    expected_executables = candidate_executables
                    for name in REQUIRED_EXECUTABLES:
                        if not isinstance(expected_executables.get(name), dict):
                            invalid_containers.append(f"executables.{name}")
                try:
                    expected_font_runtime = validate_font_runtime(
                        candidate_font_runtime
                    )
                except ValueError:
                    invalid_containers.append("font_runtime")
                if expected_font_runtime is not None and expected_font_runtime[
                    "provider"
                ] == "fontconfig":
                    if not isinstance(candidate_fontconfig, dict):
                        invalid_containers.append("fontconfig")
                    else:
                        expected_fontconfig = candidate_fontconfig
                elif candidate_fontconfig is not None:
                    invalid_containers.append("fontconfig")
                elif candidate_fontconfig is None:
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
                keys: list[tuple[str, Any, Any]] = [
                    (
                        "renderer_backend",
                        result.get("renderer_backend"),
                        expected.get("renderer_backend"),
                    ),
                    (
                        "preview_size",
                        result.get("preview_size"),
                        expected.get("preview_size"),
                    ),
                    (
                        "font_runtime",
                        font_runtime_identity(result.get("font_runtime"))
                        if result.get("font_runtime") is not None
                        else None,
                        font_runtime_identity(expected_font_runtime),
                    ),
                ]
                if expected_font_runtime["provider"] == "fontconfig":
                    keys.append(
                        (
                            "fontconfig.sha256",
                            result["fontconfig"].get("sha256")
                            if isinstance(result["fontconfig"], dict)
                            else None,
                            expected_fontconfig.get("sha256"),
                        )
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
    parser.add_argument("--pdftoppm", default="pdftoppm")
    parser.add_argument("--pdffonts", default="pdffonts")
    parser.add_argument("--pdftotext", default="pdftotext")
    parser.add_argument(
        "--font-family",
        choices=("Hiragino Sans GB", "Microsoft YaHei", "STKaiti"),
        default=None,
    )
    parser.add_argument(
        "--font-provider",
        choices=("auto", "fontconfig", "windows-system"),
        default="auto",
    )
    parser.add_argument("--fontconfig", type=Path)
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
