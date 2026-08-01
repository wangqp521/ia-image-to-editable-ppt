#!/usr/bin/env python3
"""Build, verify, and publish a reproducible no-overwrite Skill bundle."""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable


SCRIPT_ROOT = Path(__file__).resolve().parent
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

try:
    from scripts.lib.error_codes import ToolError  # noqa: E402
    from scripts.lib.path_contracts import find_user_controlled_symlink  # noqa: E402
except ModuleNotFoundError:
    from lib.error_codes import ToolError  # type: ignore[no-redef]  # noqa: E402
    from lib.path_contracts import find_user_controlled_symlink  # type: ignore[no-redef]  # noqa: E402


EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".worktrees",
        ".superpowers",
        "docs",
        ".canary",
        ".release-staging",
        "__pycache__",
    }
)
MANIFEST_NAME = "release-manifest.json"
REQUIRED_METADATA = (
    "git_commit",
    "capability_manifest_sha256",
    "test_count",
    "canary_commit",
)
_GIT_HASH = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_BeforePublish = Callable[[Path, dict[str, str]], None]
_DARWIN_RENAME_EXCL = 0x00000004
_LINUX_RENAME_NOREPLACE = 1


def _error(code: str, path: str | Path, detail: str) -> ToolError:
    return ToolError(code, str(path), detail)


def _identity(value: os.stat_result) -> tuple[int, int]:
    return (value.st_dev, value.st_ino)


def _path_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise _error(
            "RELEASE_DESTINATION_UNSAFE",
            path,
            "cannot inspect release destination",
        ) from exc
    return True


def _validated_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(metadata, dict) or set(metadata) != set(REQUIRED_METADATA):
        raise _error(
            "RELEASE_METADATA_INVALID",
            "metadata",
            "metadata must contain exactly the four release identity fields",
        )
    if not isinstance(metadata["git_commit"], str) or not _GIT_HASH.fullmatch(
        metadata["git_commit"]
    ):
        raise _error(
            "RELEASE_METADATA_INVALID",
            "metadata.git_commit",
            "git_commit must be a lowercase 40-character Git object id",
        )
    capability_hash = metadata["capability_manifest_sha256"]
    if not isinstance(capability_hash, str) or not _SHA256.fullmatch(
        capability_hash
    ):
        raise _error(
            "RELEASE_METADATA_INVALID",
            "metadata.capability_manifest_sha256",
            "capability_manifest_sha256 must be a lowercase SHA-256 digest",
        )
    test_count = metadata["test_count"]
    if isinstance(test_count, bool) or not isinstance(test_count, int) or test_count <= 0:
        raise _error(
            "RELEASE_METADATA_INVALID",
            "metadata.test_count",
            "test_count must be a positive integer",
        )
    if not isinstance(metadata["canary_commit"], str) or not _GIT_HASH.fullmatch(
        metadata["canary_commit"]
    ):
        raise _error(
            "RELEASE_METADATA_INVALID",
            "metadata.canary_commit",
            "canary_commit must be a lowercase 40-character Git object id",
        )
    return {key: metadata[key] for key in REQUIRED_METADATA}


def _unsafe_source(path: Path, detail: str) -> ToolError:
    return _error("RELEASE_SOURCE_UNSAFE", path, detail)


def _require_dirfd_primitives(path: Path) -> None:
    required_flags = ("O_DIRECTORY", "O_NOFOLLOW")
    if any(not hasattr(os, flag) for flag in required_flags):
        raise _unsafe_source(
            path,
            "platform lacks required descriptor-relative no-follow primitives",
        )
    if os.open not in os.supports_dir_fd or os.stat not in os.supports_dir_fd:
        raise _unsafe_source(
            path,
            "platform lacks required descriptor-relative filesystem operations",
        )


def _open_directory(
    path: Path,
) -> tuple[int, tuple[int, int]]:
    _require_dirfd_primitives(path)
    try:
        before = path.lstat()
    except OSError as exc:
        raise _unsafe_source(path, "cannot inspect source directory") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
        raise _unsafe_source(path, "release source must be a non-symlink directory")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise _unsafe_source(path, "cannot safely open source directory") from exc
    opened = os.fstat(descriptor)
    if not stat.S_ISDIR(opened.st_mode) or _identity(opened) != _identity(before):
        os.close(descriptor)
        raise _unsafe_source(path, "source directory changed while being opened")
    return descriptor, _identity(opened)


def _entry_stat(directory_fd: int, name: str, path: Path) -> os.stat_result:
    try:
        value = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as exc:
        raise _unsafe_source(path, "cannot inspect source node") from exc
    if stat.S_ISLNK(value.st_mode):
        raise _unsafe_source(path, "source contains a symbolic link")
    if not (stat.S_ISDIR(value.st_mode) or stat.S_ISREG(value.st_mode)):
        raise _unsafe_source(path, "source contains a non-regular filesystem node")
    return value


def _open_directory_at(
    parent_fd: int,
    name: str,
    path: Path,
    expected: os.stat_result | None = None,
) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise _unsafe_source(path, "cannot safely open source directory") from exc
    opened = os.fstat(descriptor)
    if not stat.S_ISDIR(opened.st_mode) or (
        expected is not None and _identity(opened) != _identity(expected)
    ):
        os.close(descriptor)
        raise _unsafe_source(path, "source directory changed while being opened")
    return descriptor


@dataclass(frozen=True)
class _SourceInventory:
    directories: tuple[Path, ...]
    files: tuple[Path, ...]
    directory_identities: dict[str, tuple[int, int]]
    file_identities: dict[str, tuple[int, int]]


def _collect_source(
    root_fd: int,
    source: Path,
) -> _SourceInventory:
    included_directories: list[Path] = []
    included_files: list[Path] = []
    directory_identities: dict[str, tuple[int, int]] = {}
    file_identities: dict[str, tuple[int, int]] = {}

    def visit(directory_fd: int, relative: Path, excluded: bool) -> None:
        try:
            names = sorted(os.listdir(directory_fd))
        except OSError as exc:
            raise _unsafe_source(
                source / relative, "cannot enumerate source directory"
            ) from exc
        for name in names:
            child_relative = relative / name
            child_path = source / child_relative
            value = _entry_stat(directory_fd, name, child_path)
            child_excluded = excluded or name in EXCLUDED_DIRS
            if stat.S_ISDIR(value.st_mode):
                child_fd = _open_directory_at(
                    directory_fd, name, child_path, value
                )
                try:
                    if not child_excluded:
                        included_directories.append(child_relative)
                        directory_identities[child_relative.as_posix()] = _identity(
                            value
                        )
                    visit(child_fd, child_relative, child_excluded)
                finally:
                    os.close(child_fd)
                continue
            if (
                not child_excluded
                and not name.endswith(".pyc")
                and name != MANIFEST_NAME
            ):
                included_files.append(child_relative)
                file_identities[child_relative.as_posix()] = _identity(value)

    visit(root_fd, Path(), False)
    return _SourceInventory(
        directories=tuple(
            sorted(included_directories, key=lambda path: path.as_posix())
        ),
        files=tuple(sorted(included_files, key=lambda path: path.as_posix())),
        directory_identities=directory_identities,
        file_identities=file_identities,
    )


def _open_parent_at(
    root_fd: int,
    relative: Path,
    source: Path,
    directory_identities: dict[str, tuple[int, int]],
) -> int:
    descriptor = os.dup(root_fd)
    current = Path()
    try:
        for component in relative.parts:
            current /= component
            expected = _entry_stat(descriptor, component, source / current)
            if not stat.S_ISDIR(expected.st_mode):
                raise _unsafe_source(
                    source / current, "source ancestor is not a directory"
                )
            if _identity(expected) != directory_identities.get(current.as_posix()):
                raise _unsafe_source(
                    source / current,
                    "source ancestor identity changed after traversal",
                )
            child_fd = _open_directory_at(
                descriptor, component, source / current, expected
            )
            os.close(descriptor)
            descriptor = child_fd
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _copy_regular_file_at(
    root_fd: int,
    source: Path,
    relative: Path,
    destination_root_fd: int,
    directory_identities: dict[str, tuple[int, int]],
    file_identities: dict[str, tuple[int, int]],
) -> None:
    parent_fd = _open_parent_at(
        root_fd, relative.parent, source, directory_identities
    )
    flags = os.O_RDONLY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        expected = _entry_stat(parent_fd, relative.name, source / relative)
        if not stat.S_ISREG(expected.st_mode):
            raise _unsafe_source(
                source / relative, "included source node is not a regular file"
            )
        if _identity(expected) != file_identities.get(relative.as_posix()):
            raise _unsafe_source(
                source / relative,
                "source file identity changed after traversal",
            )
        try:
            descriptor = os.open(relative.name, flags, dir_fd=parent_fd)
        except OSError as exc:
            raise _unsafe_source(
                source / relative, "cannot safely open source file"
            ) from exc
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or _identity(opened) != _identity(expected)
            ):
                raise _unsafe_source(
                    source / relative,
                    "source file changed while being opened",
                )
            destination_parent_fd = _open_owned_parent_at(
                destination_root_fd, relative.parent
            )
            try:
                output_descriptor = os.open(
                    relative.name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=destination_parent_fd,
                )
            except OSError as exc:
                os.close(destination_parent_fd)
                raise _error(
                    "RELEASE_BUILD_FAILED",
                    relative.as_posix(),
                    "cannot create staged payload file",
                ) from exc
            try:
                with os.fdopen(descriptor, "rb", closefd=False) as input_stream:
                    with os.fdopen(
                        output_descriptor, "wb", closefd=False
                    ) as output_stream:
                        shutil.copyfileobj(input_stream, output_stream)
                        output_stream.flush()
                        os.fsync(output_stream.fileno())
                os.fchmod(output_descriptor, stat.S_IMODE(opened.st_mode))
            finally:
                os.close(output_descriptor)
                os.close(destination_parent_fd)
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_fd)


def _open_owned_parent_at(root_fd: int, relative: Path) -> int:
    descriptor = os.dup(root_fd)
    try:
        for component in relative.parts:
            value = os.stat(
                component, dir_fd=descriptor, follow_symlinks=False
            )
            if stat.S_ISLNK(value.st_mode) or not stat.S_ISDIR(value.st_mode):
                raise _error(
                    "RELEASE_BUILD_FAILED",
                    relative.as_posix(),
                    "staged payload ancestor is unsafe",
                )
            child_fd = _open_directory_at(
                descriptor, component, relative, value
            )
            os.close(descriptor)
            descriptor = child_fd
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _mkdir_owned_at(root_fd: int, relative: Path) -> None:
    parent_fd = _open_owned_parent_at(root_fd, relative.parent)
    try:
        os.mkdir(relative.name, mode=0o700, dir_fd=parent_fd)
    except OSError as exc:
        raise _error(
            "RELEASE_BUILD_FAILED",
            relative.as_posix(),
            "cannot create staged payload directory",
        ) from exc
    finally:
        os.close(parent_fd)


def _hash_regular_file_at(
    directory_fd: int,
    name: str,
    expected: os.stat_result,
    audit_path: str,
) -> str:
    descriptor = os.open(
        name,
        os.O_RDONLY | os.O_NOFOLLOW,
        dir_fd=directory_fd,
    )
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or _identity(opened) != _identity(expected)
        ):
            raise _error(
                "RELEASE_BUILD_FAILED",
                audit_path,
                "staged payload file identity changed",
            )
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _payload_hashes(root_fd: int) -> dict[str, str]:
    payload: dict[str, str] = {}

    def visit(directory_fd: int, relative: PurePosixPath) -> None:
        for name in sorted(os.listdir(directory_fd)):
            value = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            child = relative / name
            audit_path = child.as_posix()
            if stat.S_ISLNK(value.st_mode):
                raise _error(
                    "RELEASE_BUILD_FAILED",
                    audit_path,
                    "staged payload contains a symbolic link",
                )
            if stat.S_ISDIR(value.st_mode):
                child_fd = _open_directory_at(
                    directory_fd, name, Path(audit_path), value
                )
                try:
                    visit(child_fd, child)
                finally:
                    os.close(child_fd)
                continue
            if not stat.S_ISREG(value.st_mode):
                raise _error(
                    "RELEASE_BUILD_FAILED",
                    audit_path,
                    "staged payload contains a non-regular file",
                )
            if audit_path != MANIFEST_NAME:
                payload[audit_path] = _hash_regular_file_at(
                    directory_fd, name, value, audit_path
                )

    visit(root_fd, PurePosixPath())
    return {key: payload[key] for key in sorted(payload)}


def _write_bytes_no_overwrite_at(
    directory_fd: int,
    name: str,
    value: bytes,
) -> None:
    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory_fd,
    )
    try:
        offset = 0
        while offset < len(value):
            offset += os.write(descriptor, value[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_bytes_at(directory_fd: int, name: str) -> bytes:
    expected = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    descriptor = os.open(
        name,
        os.O_RDONLY | os.O_NOFOLLOW,
        dir_fd=directory_fd,
    )
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or _identity(opened) != _identity(expected)
        ):
            raise _error(
                "RELEASE_BUILD_FAILED",
                name,
                "staged manifest identity changed",
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _canonical_json_bytes(value: dict[str, Any]) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, UnicodeError, ValueError) as exc:
        raise _error(
            "RELEASE_METADATA_INVALID",
            "metadata",
            "release identity must be canonical UTF-8 JSON",
        ) from exc


def _validate_component(name: str) -> bytes:
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\0" in name
    ):
        raise ValueError("publication names must be single path components")
    return os.fsencode(name)


def _rename_no_replace_at(
    source_parent_fd: int,
    source_name: str,
    destination_parent_fd: int,
    destination_name: str,
) -> None:
    """Atomically rename one dirfd-relative entry without replacement."""
    source = _validate_component(source_name)
    destination = _validate_component(destination_name)
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        primitive = getattr(library, "renameatx_np", None)
        flags = _DARWIN_RENAME_EXCL
    elif sys.platform.startswith("linux"):
        primitive = getattr(library, "renameat2", None)
        flags = _LINUX_RENAME_NOREPLACE
    else:
        primitive = None
        flags = 0
    if primitive is None:
        raise NotImplementedError(
            "platform lacks dirfd atomic no-replace directory publication"
        )
    primitive.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    primitive.restype = ctypes.c_int
    ctypes.set_errno(0)
    result = primitive(
        source_parent_fd,
        source,
        destination_parent_fd,
        destination,
        flags,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError(
            error_number, os.strerror(error_number), destination_name
        )
    unsupported = {
        errno.EINVAL,
        errno.ENOSYS,
        getattr(errno, "ENOTSUP", errno.EINVAL),
        getattr(errno, "EOPNOTSUPP", errno.EINVAL),
    }
    if error_number in unsupported:
        raise NotImplementedError(
            "filesystem lacks dirfd atomic no-replace directory publication"
        )
    raise OSError(error_number, os.strerror(error_number), destination_name)


def _validate_secure_parent_fd(
    descriptor: int,
    expected_identity: tuple[int, int],
    audit_path: Path,
) -> None:
    try:
        current = os.fstat(descriptor)
    except OSError as exc:
        raise _error(
            "RELEASE_DESTINATION_UNSAFE",
            audit_path,
            "cannot inspect release destination parent descriptor",
        ) from exc
    if (
        not stat.S_ISDIR(current.st_mode)
        or _identity(current) != expected_identity
        or current.st_uid != os.getuid()
        or stat.S_IMODE(current.st_mode) & 0o022
    ):
        raise _error(
            "RELEASE_DESTINATION_UNSAFE",
            audit_path,
            "release destination parent must retain its identity, be owned by "
            "the current uid, and not be group/world writable",
        )


def _prepare_parent(destination: Path) -> tuple[Path, int, tuple[int, int]]:
    parent = destination.parent
    existed = _path_exists(parent)
    try:
        parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if not existed:
            os.chmod(parent, 0o700, follow_symlinks=False)
    except OSError as exc:
        raise _error(
            "RELEASE_DESTINATION_UNSAFE",
            parent,
            "cannot create a private release destination parent",
        ) from exc
    symlink = find_user_controlled_symlink(parent)
    if symlink is not None:
        raise _error(
            "RELEASE_DESTINATION_UNSAFE",
            symlink,
            "release destination parent must not contain a symlink",
        )
    try:
        before = parent.lstat()
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        descriptor = os.open(parent, flags)
        opened = os.fstat(descriptor)
    except OSError as exc:
        raise _error(
            "RELEASE_DESTINATION_UNSAFE",
            parent,
            "cannot safely open release destination parent",
        ) from exc
    identity = _identity(opened)
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISDIR(before.st_mode)
        or _identity(before) != identity
    ):
        os.close(descriptor)
        raise _error(
            "RELEASE_DESTINATION_UNSAFE",
            parent,
            "release destination parent changed while being opened",
        )
    try:
        _validate_secure_parent_fd(descriptor, identity, parent)
    except BaseException:
        os.close(descriptor)
        raise
    return parent, descriptor, identity


@dataclass(frozen=True)
class _PublicationTransaction:
    parent: Path
    parent_fd: int
    parent_identity: tuple[int, int]
    envelope_name: str
    envelope_fd: int
    envelope_identity: tuple[int, int]
    payload_name: str
    payload_fd: int
    payload_identity: tuple[int, int]


def _open_private_directory_at(
    parent_fd: int,
    name: str,
    expected: os.stat_result,
    audit_path: Path,
) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
        opened = os.fstat(descriptor)
    except OSError as exc:
        raise _error(
            "RELEASE_BUILD_FAILED",
            audit_path,
            "cannot safely open private release transaction directory",
        ) from exc
    if (
        not stat.S_ISDIR(opened.st_mode)
        or _identity(opened) != _identity(expected)
        or opened.st_uid != os.getuid()
        or stat.S_IMODE(opened.st_mode) & 0o077
    ):
        os.close(descriptor)
        raise _error(
            "RELEASE_BUILD_FAILED",
            audit_path,
            "private release transaction directory is unsafe",
        )
    try:
        os.fchmod(descriptor, 0o700)
    except OSError as exc:
        os.close(descriptor)
        raise _error(
            "RELEASE_BUILD_FAILED",
            audit_path,
            "cannot enforce mode 0700 on release transaction directory",
        ) from exc
    return descriptor


def _create_transaction(
    parent: Path,
    parent_fd: int,
    parent_identity: tuple[int, int],
    destination: Path,
) -> _PublicationTransaction:
    for _ in range(16):
        envelope_name = f".{destination.name}.{secrets.token_hex(16)}.txn"
        try:
            os.mkdir(envelope_name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            continue
        except OSError as exc:
            raise _error(
                "RELEASE_BUILD_FAILED",
                parent,
                "cannot create private release transaction envelope",
            ) from exc
        envelope_path = parent / envelope_name
        envelope_value = os.stat(
            envelope_name, dir_fd=parent_fd, follow_symlinks=False
        )
        envelope_fd = _open_private_directory_at(
            parent_fd, envelope_name, envelope_value, envelope_path
        )
        try:
            os.mkdir("payload", mode=0o700, dir_fd=envelope_fd)
            payload_value = os.stat(
                "payload", dir_fd=envelope_fd, follow_symlinks=False
            )
            payload_fd = _open_private_directory_at(
                envelope_fd,
                "payload",
                payload_value,
                envelope_path / "payload",
            )
        except BaseException:
            os.close(envelope_fd)
            raise
        return _PublicationTransaction(
            parent=parent,
            parent_fd=parent_fd,
            parent_identity=parent_identity,
            envelope_name=envelope_name,
            envelope_fd=envelope_fd,
            envelope_identity=_identity(envelope_value),
            payload_name="payload",
            payload_fd=payload_fd,
            payload_identity=_identity(payload_value),
        )
    raise _error(
        "RELEASE_BUILD_FAILED",
        parent,
        "cannot allocate a unique private release transaction envelope",
    )


def _remove_owned_tree(directory_fd: int) -> None:
    for name in os.listdir(directory_fd):
        value = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(value.st_mode):
            child_fd = _open_directory_at(
                directory_fd, name, Path(name), value
            )
            try:
                _remove_owned_tree(child_fd)
            finally:
                os.close(child_fd)
            os.rmdir(name, dir_fd=directory_fd)
        else:
            os.unlink(name, dir_fd=directory_fd)


def _identity_names(
    parent_fd: int,
    expected_identity: tuple[int, int],
) -> list[str]:
    matches: list[str] = []
    try:
        names = os.listdir(parent_fd)
    except OSError:
        return matches
    for name in names:
        try:
            value = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError:
            continue
        if _identity(value) == expected_identity:
            matches.append(name)
    return sorted(matches)


def _cleanup_incomplete(
    transaction: _PublicationTransaction,
    detail: str,
) -> ToolError:
    aliases = _identity_names(
        transaction.parent_fd, transaction.envelope_identity
    )
    residue = ", ".join(aliases) if aliases else "<identity not found>"
    return _error(
        "RELEASE_CLEANUP_INCOMPLETE",
        transaction.parent / transaction.envelope_name,
        f"{detail}; transaction residue: {residue}",
    )


def _cleanup_transaction(
    transaction: _PublicationTransaction,
    payload_published: bool,
) -> None:
    try:
        _validate_secure_parent_fd(
            transaction.parent_fd,
            transaction.parent_identity,
            transaction.parent,
        )
        current_envelope = os.stat(
            transaction.envelope_name,
            dir_fd=transaction.parent_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(current_envelope.st_mode)
            or _identity(current_envelope) != transaction.envelope_identity
        ):
            raise _cleanup_incomplete(
                transaction,
                "transaction envelope identity changed; unknown entry preserved",
            )
        if not payload_published:
            current_payload = os.stat(
                transaction.payload_name,
                dir_fd=transaction.envelope_fd,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISDIR(current_payload.st_mode)
                or _identity(current_payload) != transaction.payload_identity
            ):
                raise _cleanup_incomplete(
                    transaction,
                    "transaction payload identity changed; unknown entry preserved",
                )
            _remove_owned_tree(transaction.payload_fd)
            current_payload = os.stat(
                transaction.payload_name,
                dir_fd=transaction.envelope_fd,
                follow_symlinks=False,
            )
            if _identity(current_payload) != transaction.payload_identity:
                raise _cleanup_incomplete(
                    transaction,
                    "transaction payload changed during cleanup",
                )
            os.rmdir(
                transaction.payload_name,
                dir_fd=transaction.envelope_fd,
            )
        else:
            try:
                os.stat(
                    transaction.payload_name,
                    dir_fd=transaction.envelope_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                raise _cleanup_incomplete(
                    transaction,
                    "published payload name remains inside transaction envelope",
                )
        current_envelope = os.stat(
            transaction.envelope_name,
            dir_fd=transaction.parent_fd,
            follow_symlinks=False,
        )
        if _identity(current_envelope) != transaction.envelope_identity:
            raise _cleanup_incomplete(
                transaction,
                "transaction envelope changed during cleanup",
            )
        _validate_secure_parent_fd(
            transaction.parent_fd,
            transaction.parent_identity,
            transaction.parent,
        )
        os.rmdir(
            transaction.envelope_name,
            dir_fd=transaction.parent_fd,
        )
    except ToolError as exc:
        if exc.code == "RELEASE_CLEANUP_INCOMPLETE":
            raise
        raise _cleanup_incomplete(
            transaction, f"cleanup safety validation failed: {exc.detail}"
        ) from exc
    except OSError as exc:
        raise _cleanup_incomplete(
            transaction, f"descriptor-relative cleanup failed: {exc}"
        ) from exc


def _verify_path_identity(
    path: Path,
    expected: tuple[int, int],
    *,
    code: str,
    detail: str,
) -> None:
    try:
        current = path.lstat()
    except OSError as exc:
        raise _error(code, path, detail) from exc
    if not stat.S_ISDIR(current.st_mode) or _identity(current) != expected:
        raise _error(code, path, detail)


def _create_release_bundle(
    source: Path,
    destination: Path,
    metadata: dict[str, Any],
    before_publish: _BeforePublish | None,
) -> dict[str, Any]:
    source = Path(source)
    destination = Path(destination)
    identity = _validated_metadata(metadata)
    _validate_component(destination.name)

    source_fd, source_identity = _open_directory(source)
    parent_fd: int | None = None
    transaction: _PublicationTransaction | None = None
    payload_published = False
    manifest_result: dict[str, Any] | None = None
    primary_error: BaseException | None = None
    try:
        inventory = _collect_source(source_fd, source)
        parent, parent_fd, parent_identity = _prepare_parent(destination)
        try:
            os.stat(
                destination.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise _error(
                "RELEASE_DESTINATION_UNSAFE",
                destination,
                "cannot inspect release destination through its parent descriptor",
            ) from exc
        else:
            raise _error(
                "RELEASE_DESTINATION_EXISTS",
                destination,
                "release destination already exists",
            )
        transaction = _create_transaction(
            parent,
            parent_fd,
            parent_identity,
            destination,
        )
        for relative in inventory.directories:
            _mkdir_owned_at(transaction.payload_fd, relative)
        for relative in inventory.files:
            _copy_regular_file_at(
                source_fd,
                source,
                relative,
                transaction.payload_fd,
                inventory.directory_identities,
                inventory.file_identities,
            )

        expected_paths = [relative.as_posix() for relative in inventory.files]
        payload = _payload_hashes(transaction.payload_fd)
        if list(payload) != expected_paths:
            raise _error(
                "RELEASE_BUILD_FAILED",
                transaction.parent / transaction.envelope_name,
                "staged payload file set changed during bundle creation",
            )
        manifest = {**identity, "files": payload}
        manifest_bytes = _canonical_json_bytes(manifest)
        _write_bytes_no_overwrite_at(
            transaction.payload_fd,
            MANIFEST_NAME,
            manifest_bytes,
        )

        if _payload_hashes(transaction.payload_fd) != payload:
            raise _error(
                "RELEASE_BUILD_FAILED",
                transaction.parent / transaction.envelope_name,
                "independent staged payload verification failed",
            )
        if (
            _read_bytes_at(transaction.payload_fd, MANIFEST_NAME)
            != manifest_bytes
        ):
            raise _error(
                "RELEASE_BUILD_FAILED",
                transaction.parent / transaction.envelope_name / MANIFEST_NAME,
                "canonical release manifest verification failed",
            )
        _verify_path_identity(
            source,
            source_identity,
            code="RELEASE_SOURCE_UNSAFE",
            detail="release source identity changed during bundle creation",
        )
        _validate_secure_parent_fd(
            parent_fd,
            parent_identity,
            parent,
        )
        current_envelope = os.stat(
            transaction.envelope_name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(current_envelope.st_mode)
            or _identity(current_envelope) != transaction.envelope_identity
        ):
            raise _error(
                "RELEASE_BUILD_FAILED",
                transaction.parent / transaction.envelope_name,
                "private release transaction envelope identity changed",
            )
        current_payload = os.stat(
            transaction.payload_name,
            dir_fd=transaction.envelope_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(current_payload.st_mode)
            or _identity(current_payload) != transaction.payload_identity
        ):
            raise _error(
                "RELEASE_BUILD_FAILED",
                transaction.parent
                / transaction.envelope_name
                / transaction.payload_name,
                "private release payload identity changed",
            )
        if before_publish is not None:
            before_publish(
                transaction.parent
                / transaction.envelope_name
                / transaction.payload_name,
                payload,
            )
        try:
            _rename_no_replace_at(
                transaction.envelope_fd,
                transaction.payload_name,
                transaction.parent_fd,
                destination.name,
            )
        except FileExistsError as exc:
            raise _error(
                "RELEASE_DESTINATION_EXISTS",
                destination,
                "release destination won a concurrent publication race",
            ) from exc
        except NotImplementedError as exc:
            raise _error(
                "RELEASE_PUBLISH_UNSUPPORTED",
                destination,
                "filesystem lacks atomic no-replace directory publication",
            ) from exc
        except OSError as exc:
            raise _error(
                "RELEASE_PUBLISH_FAILED",
                destination,
                "cannot atomically publish release bundle",
            ) from exc
        payload_published = True
        manifest_result = manifest
    except BaseException as exc:
        primary_error = exc

    cleanup_error: ToolError | None = None
    if transaction is not None:
        try:
            _cleanup_transaction(transaction, payload_published)
        except ToolError as exc:
            cleanup_error = exc
        finally:
            os.close(transaction.payload_fd)
            os.close(transaction.envelope_fd)
    os.close(source_fd)
    if parent_fd is not None:
        os.close(parent_fd)

    if cleanup_error is not None:
        if primary_error is not None:
            raise cleanup_error from primary_error
        raise cleanup_error
    if primary_error is not None:
        raise primary_error
    if manifest_result is None:
        raise RuntimeError("release publication completed without a manifest")
    return manifest_result


def create_release_bundle(
    source: Path,
    destination: Path,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """Build, verify, and publish a deterministic no-overwrite Skill bundle."""
    return _create_release_bundle(source, destination, metadata, None)


def _git_process(
    source: Path,
    *arguments: str,
    text: bool = True,
    timeout: int = 30,
) -> subprocess.CompletedProcess[Any]:
    try:
        return subprocess.run(
            ["git", "-C", str(source), *arguments],
            check=False,
            capture_output=True,
            text=text,
            encoding="utf-8" if text else None,
            errors="strict" if text else None,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise _error(
            "RELEASE_SOURCE_NOT_GIT",
            source,
            "cannot query release Git worktree",
        ) from exc


def _git_output(source: Path, *arguments: str) -> str:
    completed = _git_process(source, *arguments)
    value = completed.stdout.strip()
    if completed.returncode != 0 or not value:
        raise _error(
            "RELEASE_SOURCE_NOT_GIT",
            source,
            "Git identity query failed",
        )
    return value


def _is_excluded_relative(value: str) -> bool:
    normalized = value.rstrip("/")
    if not normalized:
        return True
    path = PurePosixPath(normalized)
    return (
        any(part in EXCLUDED_DIRS for part in path.parts)
        or path.name.endswith(".pyc")
        or path.name == MANIFEST_NAME
    )


def _git_status_violations(source: Path) -> list[str]:
    completed = _git_process(
        source,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--ignored=matching",
        text=False,
    )
    if completed.returncode != 0:
        raise _error(
            "RELEASE_SOURCE_NOT_GIT",
            source,
            "cannot inspect release Git worktree status",
        )
    violations: list[str] = []
    records = completed.stdout.split(b"\0")
    for record in records:
        if not record:
            continue
        if len(record) < 4:
            violations.append("<malformed-status>")
            continue
        status_code = record[:2].decode("ascii", errors="replace")
        path = os.fsdecode(record[3:])
        if status_code in {"??", "!!"} and _is_excluded_relative(path):
            continue
        violations.append(f"{status_code} {path}")
    return violations


def _archive_identity(
    source: Path,
    head: str,
) -> tuple[bytes, dict[str, str]]:
    completed = _git_process(
        source, "archive", "--format=tar", head, text=False, timeout=120
    )
    if completed.returncode != 0:
        raise _error(
            "RELEASE_SOURCE_NOT_GIT",
            source,
            "cannot snapshot release Git tree",
        )
    archive = completed.stdout
    payload: dict[str, str] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
            for member in tar:
                if member.isdir() or _is_excluded_relative(member.name):
                    continue
                if not member.isfile():
                    raise _unsafe_source(
                        source / member.name,
                        "fixed Git tree contains a non-regular payload node",
                    )
                stream = tar.extractfile(member)
                if stream is None:
                    raise _error(
                        "RELEASE_SOURCE_NOT_GIT",
                        source / member.name,
                        "cannot read fixed Git payload",
                    )
                payload[member.name] = hashlib.sha256(stream.read()).hexdigest()
    except (OSError, tarfile.TarError) as exc:
        raise _error(
            "RELEASE_SOURCE_NOT_GIT",
            source,
            "cannot inspect fixed Git tree",
        ) from exc
    return archive, {key: payload[key] for key in sorted(payload)}


@dataclass(frozen=True)
class _GitSourceGuard:
    source: Path
    source_identity: tuple[int, int]
    head: str
    archive: bytes
    expected_payload: dict[str, str]

    @classmethod
    def capture(cls, source: Path) -> _GitSourceGuard:
        raw_source = Path(source)
        descriptor, source_identity = _open_directory(raw_source)
        os.close(descriptor)
        source = raw_source.resolve(strict=True)
        top_level = Path(
            _git_output(source, "rev-parse", "--show-toplevel")
        ).resolve(strict=True)
        if top_level != source:
            raise _error(
                "RELEASE_SOURCE_NOT_GIT",
                source,
                "--source must be the Git worktree root",
            )
        inside = _git_output(source, "rev-parse", "--is-inside-work-tree")
        if inside != "true":
            raise _error(
                "RELEASE_SOURCE_NOT_GIT",
                source,
                "--source must be a Git worktree",
            )
        head = _git_output(source, "rev-parse", "--verify", "HEAD^{commit}")
        if not _GIT_HASH.fullmatch(head):
            raise _error(
                "RELEASE_SOURCE_NOT_GIT",
                source,
                "Git HEAD must resolve to a 40-character commit id",
            )
        violations = _git_status_violations(source)
        if violations:
            raise _error(
                "RELEASE_SOURCE_DIRTY",
                source,
                f"release Git worktree is not clean: {violations[0]}",
            )
        archive, expected_payload = _archive_identity(source, head)
        return cls(source, source_identity, head, archive, expected_payload)

    def verify_unchanged(self) -> None:
        _verify_path_identity(
            self.source,
            self.source_identity,
            code="RELEASE_SOURCE_CHANGED",
            detail="release source identity changed after fixed-tree capture",
        )
        head = _git_output(
            self.source, "rev-parse", "--verify", "HEAD^{commit}"
        )
        if head != self.head:
            raise _error(
                "RELEASE_SOURCE_CHANGED",
                self.source,
                "release Git HEAD changed after fixed-tree capture",
            )
        violations = _git_status_violations(self.source)
        if violations:
            raise _error(
                "RELEASE_SOURCE_CHANGED",
                self.source,
                f"release Git worktree changed: {violations[0]}",
            )

    def verify_bundle(self, _staging: Path, payload: dict[str, str]) -> None:
        self.verify_unchanged()
        if payload != self.expected_payload:
            raise _error(
                "RELEASE_SOURCE_CHANGED",
                self.source,
                "staged payload does not match the fixed Git tree",
            )


def _materialize_source_scripts(guard: _GitSourceGuard, destination: Path) -> None:
    try:
        with tarfile.open(fileobj=io.BytesIO(guard.archive), mode="r:") as tar:
            for member in tar:
                path = PurePosixPath(member.name)
                if not path.parts or path.parts[0] != "scripts":
                    continue
                if path.is_absolute() or ".." in path.parts:
                    raise _error(
                        "RELEASE_SOURCE_NOT_GIT",
                        guard.source,
                        "fixed Git tree contains an unsafe script path",
                    )
                target = destination.joinpath(*path.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    raise _unsafe_source(
                        guard.source / member.name,
                        "fixed Git scripts contain a non-regular node",
                    )
                stream = tar.extractfile(member)
                if stream is None:
                    raise _error(
                        "RELEASE_SOURCE_NOT_GIT",
                        guard.source / member.name,
                        "cannot read source-local capability registry",
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(stream.read())
    except (OSError, tarfile.TarError) as exc:
        raise _error(
            "RELEASE_SOURCE_NOT_GIT",
            guard.source,
            "cannot materialize source-local capability registry",
        ) from exc


def _materialize_fixed_tree(
    guard: _GitSourceGuard,
    destination: Path,
) -> None:
    try:
        with tarfile.open(fileobj=io.BytesIO(guard.archive), mode="r:") as tar:
            for member in tar:
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts:
                    raise _error(
                        "RELEASE_SOURCE_NOT_GIT",
                        guard.source,
                        "fixed Git tree contains an unsafe path",
                    )
                target = destination.joinpath(*path.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    raise _unsafe_source(
                        guard.source / member.name,
                        "fixed Git tree contains a non-regular node",
                    )
                stream = tar.extractfile(member)
                if stream is None:
                    raise _error(
                        "RELEASE_SOURCE_NOT_GIT",
                        guard.source / member.name,
                        "cannot read fixed Git tree member",
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(stream.read())
                os.chmod(target, member.mode & 0o777)
    except (OSError, tarfile.TarError) as exc:
        raise _error(
            "RELEASE_SOURCE_NOT_GIT",
            guard.source,
            "cannot materialize fixed Git test tree",
        ) from exc


def _source_capability_manifest_sha256(guard: _GitSourceGuard) -> str:
    with tempfile.TemporaryDirectory(prefix="release-capabilities-") as raw:
        snapshot = Path(raw)
        _materialize_source_scripts(guard, snapshot)
        code = (
            "import sys;"
            "sys.path.insert(0, sys.argv[1]);"
            "from lib.capabilities import capability_manifest_sha256;"
            "print(capability_manifest_sha256())"
        )
        try:
            completed = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    code,
                    str(snapshot / "scripts"),
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="strict",
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
            raise _error(
                "RELEASE_METADATA_INVALID",
                guard.source,
                "cannot evaluate source-local capability registry",
            ) from exc
    value = completed.stdout.strip()
    if completed.returncode != 0 or not _SHA256.fullmatch(value):
        raise _error(
            "RELEASE_METADATA_INVALID",
            guard.source,
            "source-local capability registry did not return a SHA-256 digest",
        )
    return value


def _run_test_suite(source: Path) -> int:
    tests = source / "tests"
    if tests.is_symlink() or not tests.is_dir():
        raise _error(
            "RELEASE_TESTS_FAILED",
            tests,
            "release source must contain a regular tests directory",
        )
    marker = "__RELEASE_TEST_RESULT__"
    code = """
import json
import os
import sys
import unittest

root = os.path.abspath(sys.argv[1])
os.chdir(root)
sys.path.insert(0, root)
sys.path.insert(0, os.path.join(root, "scripts"))
suite = unittest.defaultTestLoader.discover(
    os.path.join(root, "tests"),
    pattern="test_*.py",
)
result = unittest.TextTestRunner(stream=sys.stderr, verbosity=1).run(suite)
print(
    "__RELEASE_TEST_RESULT__"
    + json.dumps(
        {
            "successful": result.wasSuccessful(),
            "tests_run": result.testsRun,
        },
        sort_keys=True,
    )
)
raise SystemExit(0 if result.wasSuccessful() and result.testsRun > 0 else 1)
"""
    try:
        completed = subprocess.run(
            [sys.executable, "-I", "-c", code, str(source)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=3600,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise _error(
            "RELEASE_TESTS_FAILED",
            tests,
            "could not run the release test suite",
        ) from exc
    summary: dict[str, Any] | None = None
    for line in reversed(completed.stdout.splitlines()):
        if line.startswith(marker):
            try:
                candidate = json.loads(line[len(marker) :])
            except json.JSONDecodeError:
                break
            if isinstance(candidate, dict):
                summary = candidate
            break
    if (
        completed.returncode != 0
        or summary is None
        or summary.get("successful") is not True
        or isinstance(summary.get("tests_run"), bool)
        or not isinstance(summary.get("tests_run"), int)
        or summary["tests_run"] <= 0
    ):
        raise _error(
            "RELEASE_TESTS_FAILED",
            tests,
            "release test suite must pass before bundling"
            + (
                f": {completed.stderr[-1000:]}"
                if completed.stderr
                else ""
            ),
        )
    return summary["tests_run"]


def _canary_commit(guard: _GitSourceGuard, canary_report: Path) -> str:
    report = Path(canary_report)
    if not report.is_absolute():
        report = guard.source / report
    try:
        raw_value = report.lstat()
        if stat.S_ISLNK(raw_value.st_mode) or not stat.S_ISREG(raw_value.st_mode):
            raise ValueError("canary report is not a regular file")
        report = report.resolve(strict=True)
        relative_report = report.relative_to(guard.source)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _error(
            "RELEASE_METADATA_INVALID",
            canary_report,
            "canary report must be a committed non-symlink file within source",
        ) from exc
    value = _git_output(
        guard.source,
        "log",
        "-1",
        "--format=%H",
        guard.head,
        "--",
        relative_report.as_posix(),
    )
    if not _GIT_HASH.fullmatch(value):
        raise _error(
            "RELEASE_METADATA_INVALID",
            report,
            "canary report has no valid committed identity",
        )
    return value


def _release_context(
    source: Path,
    canary_report: Path,
) -> tuple[dict[str, Any], _GitSourceGuard]:
    guard = _GitSourceGuard.capture(source)
    capability_hash = _source_capability_manifest_sha256(guard)
    with tempfile.TemporaryDirectory(prefix="release-tests-") as raw:
        fixed_test_tree = Path(raw)
        _materialize_fixed_tree(guard, fixed_test_tree)
        test_count = _run_test_suite(fixed_test_tree)
    guard.verify_unchanged()
    metadata = _validated_metadata(
        {
            "git_commit": guard.head,
            "capability_manifest_sha256": capability_hash,
            "test_count": test_count,
            "canary_commit": _canary_commit(guard, canary_report),
        }
    )
    guard.verify_unchanged()
    return metadata, guard


def _release_metadata(source: Path, canary_report: Path) -> dict[str, Any]:
    metadata, _guard = _release_context(source, canary_report)
    return metadata


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--canary-report", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        metadata, guard = _release_context(args.source, args.canary_report)
        manifest = _create_release_bundle(
            guard.source,
            args.destination,
            metadata,
            guard.verify_bundle,
        )
    except ToolError as exc:
        print(
            json.dumps(
                {"ok": False, "errors": [exc.as_dict()]},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "ok": True,
                "destination": str(args.destination),
                "manifest": manifest,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
