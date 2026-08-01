"""Shared no-replace publication identities, receipts, locks, and rollback."""

from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import json
import os
import secrets
import stat
import sys
import threading
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


_DARWIN_RENAME_EXCL = 0x00000004
_LINUX_RENAME_NOREPLACE = 1
_LINUX_AT_FDCWD = -100
RECOVERY_MANIFEST_SCHEMA = "ia-image-to-editable-ppt/recovery-manifest"
RECOVERY_MANIFEST_VERSION = 1
RECOVERY_MAX_TOMBSTONES = 3
RECOVERY_MAX_BYTES = 64 * 1024 * 1024
RECOVERY_MANIFEST_XATTR = (
    "com.openai.ia-image-to-editable-ppt.recovery"
    if sys.platform == "darwin"
    else "user.openai.ia_image_to_editable_ppt.recovery"
)
_RECOVERY_UNSUPPORTED_PARENTS: set[tuple[int, int]] = set()


@dataclass(frozen=True)
class FileIdentity:
    device: int
    inode: int

    @classmethod
    def from_stat(cls, value: os.stat_result) -> FileIdentity:
        return cls(value.st_dev, value.st_ino)

    def as_tuple(self) -> tuple[int, int]:
        return (self.device, self.inode)

    def audit(self) -> str:
        return f"{self.device}:{self.inode}"


@dataclass(frozen=True)
class PublicationReceipt:
    destination: Path
    identity: FileIdentity
    sha256: str
    byte_count: int
    encoded: bytes


@dataclass(frozen=True)
class DirectoryPublicationReceipt:
    destination: Path
    identity: FileIdentity
    members: tuple[DirectoryMemberReceipt, ...]


@dataclass(frozen=True)
class DirectoryMemberReceipt:
    name: str
    sha256: str
    byte_count: int
    encoded: bytes


@dataclass(frozen=True)
class TombstoneReceipt:
    path: Path
    phase: str
    identity: FileIdentity
    manifest: RecoveryManifest | None = None

    def detail(self) -> str:
        return (
            f"retained_tombstone={self.path}; phase={self.phase}; "
            f"identity={self.identity.audit()}"
        )


class TransactionFailure(RuntimeError):
    def __init__(
        self,
        detail: str,
        *,
        phase: str,
        tombstone: TombstoneReceipt | None = None,
        already_exists: bool = False,
    ) -> None:
        suffix = f"; {tombstone.detail()}" if tombstone is not None else ""
        super().__init__(detail + suffix)
        self.detail = detail + suffix
        self.phase = phase
        self.tombstone = tombstone
        self.already_exists = already_exists


@dataclass(frozen=True)
class RecoveryMember:
    name: str
    sha256: str
    byte_count: int


@dataclass(frozen=True)
class RecoveryManifest:
    schema_version: int
    phase: str
    fixed_destination: Path
    tombstone_path: Path
    kind: str
    owned_identity: FileIdentity
    tombstone_identity: FileIdentity
    payload_sha256: str
    payload_size: int
    members: tuple[RecoveryMember, ...]
    competitor_state: str

    def canonical_bytes(self) -> bytes:
        payload = {
            "competitor_state": self.competitor_state,
            "fixed_destination": str(self.fixed_destination),
            "kind": self.kind,
            "members": [
                {
                    "byte_count": member.byte_count,
                    "name": member.name,
                    "sha256": member.sha256,
                }
                for member in self.members
            ],
            "owned_identity": {
                "device": self.owned_identity.device,
                "inode": self.owned_identity.inode,
            },
            "payload_sha256": self.payload_sha256,
            "payload_size": self.payload_size,
            "phase": self.phase,
            "schema": RECOVERY_MANIFEST_SCHEMA,
            "schema_version": self.schema_version,
            "tombstone_identity": {
                "device": self.tombstone_identity.device,
                "inode": self.tombstone_identity.inode,
            },
            "tombstone_path": str(self.tombstone_path),
        }
        return (
            json.dumps(
                payload,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")


def _identity_payload(value: Any) -> FileIdentity:
    if not isinstance(value, dict) or set(value) != {"device", "inode"}:
        raise ValueError("recovery identity is malformed")
    if type(value["device"]) is not int or type(value["inode"]) is not int:
        raise ValueError("recovery identity is malformed")
    return FileIdentity(value["device"], value["inode"])


def _decode_recovery_manifest(raw: bytes) -> RecoveryManifest:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("recovery manifest is not canonical UTF-8 JSON") from exc
    fields = {
        "competitor_state",
        "fixed_destination",
        "kind",
        "members",
        "owned_identity",
        "payload_sha256",
        "payload_size",
        "phase",
        "schema",
        "schema_version",
        "tombstone_identity",
        "tombstone_path",
    }
    if not isinstance(payload, dict) or set(payload) != fields:
        raise ValueError("recovery manifest fields are not exact")
    if (
        payload["schema"] != RECOVERY_MANIFEST_SCHEMA
        or payload["schema_version"] != RECOVERY_MANIFEST_VERSION
        or payload["kind"] not in {"file", "directory"}
        or not isinstance(payload["phase"], str)
        or not isinstance(payload["competitor_state"], str)
        or not isinstance(payload["fixed_destination"], str)
        or not Path(payload["fixed_destination"]).is_absolute()
        or not isinstance(payload["tombstone_path"], str)
        or not Path(payload["tombstone_path"]).is_absolute()
        or not isinstance(payload["payload_sha256"], str)
        or len(payload["payload_sha256"]) != 64
        or type(payload["payload_size"]) is not int
        or payload["payload_size"] < 0
        or not isinstance(payload["members"], list)
    ):
        raise ValueError("recovery manifest values are malformed")
    members: list[RecoveryMember] = []
    for item in payload["members"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"byte_count", "name", "sha256"}
            or not isinstance(item["name"], str)
            or Path(item["name"]).name != item["name"]
            or not isinstance(item["sha256"], str)
            or len(item["sha256"]) != 64
            or type(item["byte_count"]) is not int
            or item["byte_count"] < 0
        ):
            raise ValueError("recovery member is malformed")
        members.append(
            RecoveryMember(item["name"], item["sha256"], item["byte_count"])
        )
    if [member.name for member in members] != sorted(
        member.name for member in members
    ):
        raise ValueError("recovery members are not canonical")
    manifest = RecoveryManifest(
        schema_version=payload["schema_version"],
        phase=payload["phase"],
        fixed_destination=Path(payload["fixed_destination"]),
        tombstone_path=Path(payload["tombstone_path"]),
        kind=payload["kind"],
        owned_identity=_identity_payload(payload["owned_identity"]),
        tombstone_identity=_identity_payload(payload["tombstone_identity"]),
        payload_sha256=payload["payload_sha256"],
        payload_size=payload["payload_size"],
        members=tuple(members),
        competitor_state=payload["competitor_state"],
    )
    if manifest.canonical_bytes() != raw:
        raise ValueError("recovery manifest encoding is not canonical")
    return manifest


def _xattr_call_error(result: int, path: str) -> None:
    if result == 0:
        return
    error_number = ctypes.get_errno()
    raise OSError(error_number, os.strerror(error_number), path)


def _set_recovery_xattr(descriptor: int, name: str, value: bytes) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    encoded_name = name.encode("utf-8")
    buffer = ctypes.create_string_buffer(value)
    ctypes.set_errno(0)
    if sys.platform == "darwin":
        function = libc.fsetxattr
        function.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_uint32,
            ctypes.c_int,
        ]
        function.restype = ctypes.c_int
        result = function(
            descriptor, encoded_name, buffer, len(value), 0, 0
        )
    elif sys.platform.startswith("linux"):
        function = libc.fsetxattr
        function.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_int,
        ]
        function.restype = ctypes.c_int
        result = function(descriptor, encoded_name, buffer, len(value), 0)
    else:
        raise NotImplementedError("recovery xattrs are unsupported")
    _xattr_call_error(result, name)


def _get_recovery_xattr(descriptor: int, name: str) -> bytes:
    libc = ctypes.CDLL(None, use_errno=True)
    encoded_name = name.encode("utf-8")
    if sys.platform == "darwin":
        function = libc.fgetxattr
        function.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_uint32,
            ctypes.c_int,
        ]
        function.restype = ctypes.c_ssize_t

        def call(buffer: object, size: int) -> int:
            return int(function(descriptor, encoded_name, buffer, size, 0, 0))

    elif sys.platform.startswith("linux"):
        function = libc.fgetxattr
        function.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
        ]
        function.restype = ctypes.c_ssize_t

        def call(buffer: object, size: int) -> int:
            return int(function(descriptor, encoded_name, buffer, size))

    else:
        raise NotImplementedError("recovery xattrs are unsupported")
    ctypes.set_errno(0)
    size = call(None, 0)
    if size < 0:
        _xattr_call_error(-1, name)
    buffer = ctypes.create_string_buffer(size)
    ctypes.set_errno(0)
    actual = call(buffer, size)
    if actual < 0:
        _xattr_call_error(-1, name)
    return bytes(buffer.raw[:actual])


def _remove_recovery_xattr(descriptor: int, name: str) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    encoded_name = name.encode("utf-8")
    ctypes.set_errno(0)
    if sys.platform == "darwin":
        function = libc.fremovexattr
        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        function.restype = ctypes.c_int
        result = function(descriptor, encoded_name, 0)
    elif sys.platform.startswith("linux"):
        function = libc.fremovexattr
        function.argtypes = [ctypes.c_int, ctypes.c_char_p]
        function.restype = ctypes.c_int
        result = function(descriptor, encoded_name)
    else:
        raise NotImplementedError("recovery xattrs are unsupported")
    _xattr_call_error(result, name)


def _fsync_directory_plain(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_recovery_metadata(parent: Path) -> None:
    descriptor = os.open(
        parent,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        identity = FileIdentity.from_stat(os.fstat(descriptor)).as_tuple()
        if identity in _RECOVERY_UNSUPPORTED_PARENTS:
            raise TransactionFailure(
                "recovery metadata capability is unavailable",
                phase="recovery_preflight",
            )
        probe_name = f"{RECOVERY_MANIFEST_XATTR}.probe.{secrets.token_hex(4)}"
        try:
            _set_recovery_xattr(descriptor, probe_name, b"{}")
            if _get_recovery_xattr(descriptor, probe_name) != b"{}":
                raise OSError("recovery metadata probe changed")
            _remove_recovery_xattr(descriptor, probe_name)
            os.fsync(descriptor)
        except (AttributeError, NotImplementedError, OSError, ValueError) as exc:
            try:
                _remove_recovery_xattr(descriptor, probe_name)
            except (AttributeError, NotImplementedError, OSError, ValueError):
                pass
            _RECOVERY_UNSUPPORTED_PARENTS.add(identity)
            raise TransactionFailure(
                "recovery metadata capability is unavailable",
                phase="recovery_preflight",
            ) from exc
    finally:
        os.close(descriptor)


class DirectoryLock:
    """Serialize cooperative directory transactions without lock artifacts."""

    _local = threading.local()

    def __init__(self, path: Path):
        self.path = path
        self.descriptor: int | None = None
        self.identity: FileIdentity | None = None

    def __enter__(self) -> DirectoryLock:
        descriptor = os.open(
            self.path,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        self.descriptor = descriptor
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            identity = FileIdentity.from_stat(os.fstat(descriptor))
            held = set(getattr(self._local, "held", set()))
            held.add(identity.as_tuple())
            self._local.held = held
            self.identity = identity
            return self
        except BaseException:
            try:
                os.close(descriptor)
            finally:
                self.descriptor = None
                self.identity = None
            raise

    def __exit__(self, _type: object, _value: object, _traceback: object) -> bool:
        descriptor = self.descriptor
        identity = self.identity
        assert descriptor is not None
        assert identity is not None
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
        finally:
            self.descriptor = None
            self.identity = None
            held = set(getattr(self._local, "held", set()))
            held.discard(identity.as_tuple())
            self._local.held = held
        return False

    @classmethod
    def held_by_current_thread(cls, path: Path) -> bool:
        try:
            identity = FileIdentity.from_stat(
                os.stat(path, follow_symlinks=False)
            ).as_tuple()
        except OSError:
            return False
        return identity in getattr(cls._local, "held", set())


def _path_bytes(path: Path) -> bytes:
    value = os.fsencode(os.fspath(path))
    if b"\0" in value:
        raise ValueError("no-replace paths must not contain NUL")
    return value


def rename_no_replace(source: Path, destination: Path) -> None:
    """Atomically rename a file or directory only when destination is absent."""
    source_bytes = _path_bytes(Path(source))
    destination_bytes = _path_bytes(Path(destination))
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        try:
            rename = libc.renamex_np
        except AttributeError as exc:
            raise NotImplementedError("renamex_np(RENAME_EXCL) is unavailable") from exc
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        ctypes.set_errno(0)
        result = rename(source_bytes, destination_bytes, _DARWIN_RENAME_EXCL)
    elif sys.platform.startswith("linux"):
        try:
            rename = libc.renameat2
        except AttributeError as exc:
            raise NotImplementedError("renameat2(RENAME_NOREPLACE) is unavailable") from exc
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        ctypes.set_errno(0)
        result = rename(
            _LINUX_AT_FDCWD,
            source_bytes,
            _LINUX_AT_FDCWD,
            destination_bytes,
            _LINUX_RENAME_NOREPLACE,
        )
    else:
        raise NotImplementedError(
            f"atomic no-replace rename is unsupported on {sys.platform}"
        )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise FileExistsError(
            error_number,
            os.strerror(error_number),
            os.fspath(destination),
        )
    unsupported = {
        errno.EINVAL,
        errno.ENOSYS,
        getattr(errno, "ENOTSUP", errno.EINVAL),
        getattr(errno, "EOPNOTSUPP", errno.EINVAL),
    }
    if error_number in unsupported:
        raise NotImplementedError(
            "the filesystem does not support atomic no-replace rename"
        ) from OSError(
            error_number,
            os.strerror(error_number),
            os.fspath(destination),
        )
    raise OSError(
        error_number,
        os.strerror(error_number),
        os.fspath(destination),
    )


def rollback_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.{secrets.token_hex(16)}.rollback")


def _directory_payload_sha256(members: tuple[RecoveryMember, ...]) -> str:
    encoded = json.dumps(
        [
            {
                "byte_count": member.byte_count,
                "name": member.name,
                "sha256": member.sha256,
            }
            for member in members
        ],
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _receipt_payload(
    receipt: PublicationReceipt | DirectoryPublicationReceipt,
) -> tuple[str, str, int, tuple[RecoveryMember, ...]]:
    if isinstance(receipt, PublicationReceipt):
        return ("file", receipt.sha256, receipt.byte_count, ())
    members = tuple(
        RecoveryMember(member.name, member.sha256, member.byte_count)
        for member in receipt.members
    )
    return (
        "directory",
        _directory_payload_sha256(members),
        sum(member.byte_count for member in members),
        members,
    )


def _open_recovery_payload(path: Path, kind: str) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    if kind == "directory":
        flags |= getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        value = os.fstat(descriptor)
        expected = stat.S_ISDIR(value.st_mode) if kind == "directory" else stat.S_ISREG(value.st_mode)
        if not expected:
            raise OSError("recovery payload kind changed")
        return descriptor, value
    except BaseException:
        os.close(descriptor)
        raise


def _payload_from_descriptor(
    descriptor: int, kind: str
) -> tuple[str, int, tuple[RecoveryMember, ...]]:
    if kind == "file":
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        encoded = b"".join(chunks)
        return hashlib.sha256(encoded).hexdigest(), len(encoded), ()
    names = sorted(os.listdir(descriptor))
    members: list[RecoveryMember] = []
    for name in names:
        member = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=descriptor,
        )
        try:
            before = os.fstat(member)
            if not stat.S_ISREG(before.st_mode):
                raise OSError("recovery directory member is not regular")
            chunks: list[bytes] = []
            while True:
                chunk = os.read(member, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            after = os.fstat(member)
            if FileIdentity.from_stat(before) != FileIdentity.from_stat(after):
                raise OSError("recovery directory member changed")
            encoded = b"".join(chunks)
            members.append(
                RecoveryMember(
                    name,
                    hashlib.sha256(encoded).hexdigest(),
                    len(encoded),
                )
            )
        finally:
            os.close(member)
    result = tuple(members)
    return (
        _directory_payload_sha256(result),
        sum(member.byte_count for member in result),
        result,
    )


def _manifest_for_path(
    path: Path,
    receipt: PublicationReceipt | DirectoryPublicationReceipt,
    *,
    phase: str,
    competitor_state: str,
) -> RecoveryManifest:
    kind, expected_sha256, expected_size, expected_members = _receipt_payload(receipt)
    descriptor, value = _open_recovery_payload(path, kind)
    try:
        actual_sha256, actual_size, actual_members = _payload_from_descriptor(
            descriptor, kind
        )
    finally:
        os.close(descriptor)
    tombstone_identity = FileIdentity.from_stat(value)
    state = competitor_state
    if (
        tombstone_identity == receipt.identity
        and (
            actual_sha256 != expected_sha256
            or actual_size != expected_size
            or actual_members != expected_members
        )
    ):
        state = "owned_payload_incomplete"
    return RecoveryManifest(
        schema_version=RECOVERY_MANIFEST_VERSION,
        phase=phase,
        fixed_destination=receipt.destination,
        tombstone_path=path,
        kind=kind,
        owned_identity=receipt.identity,
        tombstone_identity=tombstone_identity,
        payload_sha256=actual_sha256,
        payload_size=actual_size,
        members=actual_members,
        competitor_state=state,
    )


def _write_manifest_descriptor(
    descriptor: int, manifest: RecoveryManifest
) -> None:
    encoded = manifest.canonical_bytes()
    _set_recovery_xattr(descriptor, RECOVERY_MANIFEST_XATTR, encoded)
    os.fsync(descriptor)
    if _get_recovery_xattr(descriptor, RECOVERY_MANIFEST_XATTR) != encoded:
        raise OSError("recovery manifest did not persist exactly")


def prepare_recovery_candidate(
    descriptor: int,
    path: Path,
    receipt: PublicationReceipt | DirectoryPublicationReceipt,
    *,
    phase: str,
) -> None:
    kind, payload_sha256, payload_size, members = _receipt_payload(receipt)
    value = os.fstat(descriptor)
    manifest = RecoveryManifest(
        schema_version=RECOVERY_MANIFEST_VERSION,
        phase=phase,
        fixed_destination=receipt.destination,
        tombstone_path=path,
        kind=kind,
        owned_identity=receipt.identity,
        tombstone_identity=FileIdentity.from_stat(value),
        payload_sha256=payload_sha256,
        payload_size=payload_size,
        members=members,
        competitor_state="none_observed",
    )
    _write_manifest_descriptor(descriptor, manifest)


def retain_recovery_tombstone(
    path: Path,
    receipt: PublicationReceipt | DirectoryPublicationReceipt,
    *,
    phase: str,
    competitor_state: str = "none_observed",
    fsync_directory: Callable[[Path], None] = _fsync_directory_plain,
) -> TombstoneReceipt:
    manifest = _manifest_for_path(
        path,
        receipt,
        phase=phase,
        competitor_state=competitor_state,
    )
    descriptor, current = _open_recovery_payload(path, manifest.kind)
    try:
        if FileIdentity.from_stat(current) != manifest.tombstone_identity:
            raise OSError("recovery tombstone identity changed")
        _write_manifest_descriptor(descriptor, manifest)
    finally:
        os.close(descriptor)
    fsync_directory(path.parent)
    return TombstoneReceipt(path, phase, manifest.tombstone_identity, manifest)


def clear_recovery_manifest(
    path: Path,
    receipt: PublicationReceipt | DirectoryPublicationReceipt,
) -> None:
    kind, _sha256, _size, _members = _receipt_payload(receipt)
    descriptor, value = _open_recovery_payload(path, kind)
    try:
        if FileIdentity.from_stat(value) != receipt.identity:
            raise OSError("publication identity changed before manifest cleanup")
        _remove_recovery_xattr(descriptor, RECOVERY_MANIFEST_XATTR)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _load_recovery_manifest(path: Path) -> RecoveryManifest:
    try:
        value = os.lstat(path)
    except OSError as exc:
        raise TransactionFailure(
            "recovery tombstone is unavailable", phase="recovery_enumerate"
        ) from exc
    if stat.S_ISLNK(value.st_mode):
        raise TransactionFailure(
            "recovery tombstone must not be a symlink",
            phase="recovery_enumerate",
        )
    kind = "directory" if stat.S_ISDIR(value.st_mode) else "file"
    if kind == "file" and not stat.S_ISREG(value.st_mode):
        raise TransactionFailure(
            "recovery tombstone kind is unsupported",
            phase="recovery_enumerate",
        )
    descriptor, opened = _open_recovery_payload(path, kind)
    try:
        try:
            raw = _get_recovery_xattr(descriptor, RECOVERY_MANIFEST_XATTR)
            manifest = _decode_recovery_manifest(raw)
        except (NotImplementedError, OSError, ValueError) as exc:
            raise TransactionFailure(
                "recovery tombstone lacks valid persistent metadata",
                phase="recovery_enumerate",
            ) from exc
        actual_identity = FileIdentity.from_stat(opened)
        if (
            manifest.tombstone_path != path
            or manifest.tombstone_identity != actual_identity
            or manifest.kind != kind
        ):
            raise TransactionFailure(
                "recovery tombstone identity does not match its manifest",
                phase="recovery_enumerate",
            )
        payload_sha256, payload_size, members = _payload_from_descriptor(
            descriptor, kind
        )
        if (
            payload_sha256 != manifest.payload_sha256
            or payload_size != manifest.payload_size
            or members != manifest.members
        ):
            raise TransactionFailure(
                "recovery tombstone payload does not match its manifest",
                phase="recovery_enumerate",
            )
        return manifest
    finally:
        os.close(descriptor)


def enumerate_recovery_manifests(directory: Path) -> tuple[RecoveryManifest, ...]:
    parent = Path(directory)
    manifests: list[RecoveryManifest] = []
    try:
        entries = sorted(
            (entry for entry in os.scandir(parent) if entry.name.endswith(".rollback")),
            key=lambda entry: entry.name,
        )
    except OSError as exc:
        raise TransactionFailure(
            "cannot enumerate recovery tombstones", phase="recovery_enumerate"
        ) from exc
    for entry in entries:
        manifests.append(_load_recovery_manifest(parent / entry.name))
    return tuple(manifests)


def enforce_recovery_budget(parent: Path, incoming_size: int) -> None:
    _ensure_recovery_metadata(parent)
    manifests = enumerate_recovery_manifests(parent)
    if len(manifests) >= RECOVERY_MAX_TOMBSTONES:
        raise TransactionFailure(
            f"recovery tombstone limit reached ({RECOVERY_MAX_TOMBSTONES})",
            phase="recovery_preflight",
        )
    retained = sum(manifest.payload_size for manifest in manifests)
    if incoming_size > RECOVERY_MAX_BYTES or retained + incoming_size > RECOVERY_MAX_BYTES:
        raise TransactionFailure(
            f"recovery tombstone capacity exceeded ({RECOVERY_MAX_BYTES} bytes)",
            phase="recovery_preflight",
        )


def _open_verified_file(path: Path) -> tuple[int, os.stat_result]:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        value = os.fstat(descriptor)
        if not stat.S_ISREG(value.st_mode):
            raise OSError("publication is not a regular file")
        return descriptor, value
    except BaseException:
        os.close(descriptor)
        raise


_FULL_STAT_FIELDS = (
    "st_dev",
    "st_ino",
    "st_mode",
    "st_size",
    "st_mtime_ns",
    "st_ctime_ns",
)


def _full_stat_snapshot(value: os.stat_result) -> tuple[int, ...]:
    return tuple(getattr(value, field) for field in _FULL_STAT_FIELDS)


def _read_all(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _verify_open_regular_file(
    descriptor: int, expected: bytes
) -> tuple[int, ...]:
    before = os.fstat(descriptor)
    before_snapshot = _full_stat_snapshot(before)
    if not stat.S_ISREG(before.st_mode) or before.st_size != len(expected):
        raise OSError("published member is not an exact regular file")
    first = _read_all(descriptor)
    middle_snapshot = _full_stat_snapshot(os.fstat(descriptor))
    os.lseek(descriptor, 0, os.SEEK_SET)
    second = _read_all(descriptor)
    after_snapshot = _full_stat_snapshot(os.fstat(descriptor))
    if (
        before_snapshot != middle_snapshot
        or before_snapshot != after_snapshot
        or first != expected
        or second != expected
    ):
        raise OSError("published file changed while verifying exact bytes")
    return before_snapshot


def verify_file_receipt(receipt: PublicationReceipt) -> None:
    descriptor: int | None = None
    try:
        descriptor, before = _open_verified_file(receipt.destination)
        if FileIdentity.from_stat(before) != receipt.identity:
            raise OSError("published file identity changed")
        if (
            len(receipt.encoded) != receipt.byte_count
            or hashlib.sha256(receipt.encoded).hexdigest() != receipt.sha256
        ):
            raise OSError("file receipt payload metadata is inconsistent")
        snapshot = _verify_open_regular_file(descriptor, receipt.encoded)
        if snapshot[:2] != receipt.identity.as_tuple():
            raise OSError("published file identity changed while verifying")
    finally:
        if descriptor is not None:
            os.close(descriptor)


def quarantine_publication(
    receipt: PublicationReceipt | DirectoryPublicationReceipt,
    *,
    phase: str,
    fsync_directory: Callable[[Path], None],
    rename: Callable[[Path, Path], None] = rename_no_replace,
) -> TombstoneReceipt | None:
    """Move current fixed path aside; restore it if it was a competitor."""
    quarantine = rollback_path(receipt.destination)
    try:
        rename(receipt.destination, quarantine)
    except FileNotFoundError:
        return None
    moved = FileIdentity.from_stat(os.stat(quarantine, follow_symlinks=False))
    fsync_directory(receipt.destination.parent)
    if moved == receipt.identity:
        return retain_recovery_tombstone(
            quarantine,
            receipt,
            phase=phase,
            fsync_directory=fsync_directory,
        )
    try:
        rename(quarantine, receipt.destination)
        fsync_directory(receipt.destination.parent)
    except FileExistsError as exc:
        tombstone = retain_recovery_tombstone(
            quarantine,
            receipt,
            phase=phase,
            competitor_state="competitor_quarantined_fixed_racer_preserved",
            fsync_directory=fsync_directory,
        )
        raise TransactionFailure(
            "competitor and newer fixed-path racer were preserved",
            phase=phase,
            tombstone=tombstone,
        ) from exc
    except (NotImplementedError, OSError, ValueError) as exc:
        tombstone = retain_recovery_tombstone(
            quarantine,
            receipt,
            phase=phase,
            competitor_state="competitor_quarantined_restore_failed",
            fsync_directory=fsync_directory,
        )
        raise TransactionFailure(
            "competing publication was preserved in rollback quarantine",
            phase=phase,
            tombstone=tombstone,
        ) from exc
    raise TransactionFailure(
        "publication identity changed before rollback; competitor restored",
        phase=phase,
    )


def directory_members_exact(
    descriptor: int, payloads: dict[str, bytes]
) -> bool:
    try:
        directory_before = _full_stat_snapshot(os.fstat(descriptor))
        expected_names = set(payloads)
        member_snapshots: dict[str, tuple[int, ...]] = {}
        for pass_number in range(2):
            if set(os.listdir(descriptor)) != expected_names:
                return False
            for name, expected in sorted(payloads.items()):
                member = os.open(
                    name,
                    os.O_RDONLY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=descriptor,
                )
                try:
                    current = _verify_open_regular_file(member, expected)
                finally:
                    os.close(member)
                if pass_number == 0:
                    member_snapshots[name] = current
                elif member_snapshots[name] != current:
                    return False
        if set(os.listdir(descriptor)) != expected_names:
            return False
        return directory_before == _full_stat_snapshot(os.fstat(descriptor))
    except OSError:
        return False


def verify_directory_receipt(receipt: DirectoryPublicationReceipt) -> None:
    """Verify fixed-directory identity, exact members, and exact member bytes."""
    descriptor: int | None = None
    try:
        descriptor = os.open(
            receipt.destination,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(before.st_mode)
            or FileIdentity.from_stat(before) != receipt.identity
        ):
            raise OSError("published directory identity changed")
        expected = {member.name: member.encoded for member in receipt.members}
        if not directory_members_exact(descriptor, expected):
            raise OSError("published directory members do not match receipt")
        after = os.fstat(descriptor)
        if _full_stat_snapshot(after) != _full_stat_snapshot(before):
            raise OSError("published directory changed while verifying")
        for member in receipt.members:
            if (
                len(member.encoded) != member.byte_count
                or hashlib.sha256(member.encoded).hexdigest() != member.sha256
            ):
                raise OSError("directory receipt payload metadata is inconsistent")
    finally:
        if descriptor is not None:
            os.close(descriptor)


def publish_directory_no_replace(
    destination: Path,
    payloads: dict[str, bytes],
    *,
    fsync_directory: Callable[[Path], None],
    rename: Callable[[Path, Path], None] = rename_no_replace,
) -> DirectoryPublicationReceipt:
    """Publish an exact directory from a creation-time-owned stable fd."""
    destination = Path(destination)
    parent = destination.parent
    temporary: Path | None = None
    descriptor: int | None = None
    identity: FileIdentity | None = None
    phase = "lock"
    published = False
    try:
        with DirectoryLock(parent):
            try:
                os.lstat(destination)
            except FileNotFoundError:
                pass
            else:
                raise TransactionFailure(
                    "output directory already exists",
                    phase="precheck",
                    already_exists=True,
                )
            incoming_size = sum(len(payload) for payload in payloads.values())
            enforce_recovery_budget(parent, incoming_size)
            phase = "staging_create"
            temporary = Path(
                tempfile.mkdtemp(
                    prefix=f".{destination.name}.",
                    suffix=".rollback",
                    dir=parent,
                )
            )
            descriptor = os.open(
                temporary,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
            identity = FileIdentity.from_stat(os.fstat(descriptor))
            planned_receipt = DirectoryPublicationReceipt(
                destination=destination,
                identity=identity,
                members=tuple(
                    DirectoryMemberReceipt(
                        name=name,
                        sha256=hashlib.sha256(payload).hexdigest(),
                        byte_count=len(payload),
                        encoded=payload,
                    )
                    for name, payload in sorted(payloads.items())
                ),
            )
            prepare_recovery_candidate(
                descriptor,
                temporary,
                planned_receipt,
                phase=phase,
            )
            fsync_directory(parent)
            phase = "payload_write"
            for name, payload in payloads.items():
                if not name or Path(name).name != name or "\0" in name:
                    raise ValueError("directory member name is unsafe")
                member = os.open(
                    name,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                    0o600,
                    dir_fd=descriptor,
                )
                try:
                    view = memoryview(payload)
                    while view:
                        written = os.write(member, view)
                        if written <= 0:
                            raise OSError("short directory payload write")
                        view = view[written:]
                    os.fsync(member)
                finally:
                    os.close(member)
            os.fsync(descriptor)
            phase = "publish"
            try:
                rename(temporary, destination)
                published = True
            except FileExistsError as exc:
                tombstone = retain_recovery_tombstone(
                    temporary,
                    planned_receipt,
                    phase=phase,
                    fsync_directory=fsync_directory,
                )
                raise TransactionFailure(
                    "output directory already exists",
                    phase=phase,
                    tombstone=tombstone,
                    already_exists=True,
                ) from exc
            phase = "parent_fsync"
            fsync_directory(parent)
            phase = "postcheck"
            current = FileIdentity.from_stat(
                os.stat(destination, follow_symlinks=False)
            )
            if current != identity or not directory_members_exact(
                descriptor, payloads
            ):
                raise OSError("published directory identity or members changed")
            phase = "manifest_clear"
            clear_recovery_manifest(destination, planned_receipt)
            fsync_directory(parent)
            return planned_receipt
    except TransactionFailure:
        raise
    except BaseException as exc:
        tombstone: TombstoneReceipt | None = None
        if published and identity is not None:
            try:
                tombstone = quarantine_publication(
                    planned_receipt,
                    phase=phase,
                    fsync_directory=fsync_directory,
                    rename=rename,
                )
            except TransactionFailure as rollback_error:
                raise rollback_error from exc
        elif temporary is not None and identity is not None:
            tombstone = retain_recovery_tombstone(
                temporary,
                planned_receipt,
                phase=phase,
                fsync_directory=fsync_directory,
            )
        raise TransactionFailure(
            "cannot atomically publish exact directory",
            phase=phase,
            tombstone=tombstone,
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def encode_json(payload: Any) -> bytes:
    return (
        json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
