"""Stable, private evidence views for reviewer-admission validation."""

from __future__ import annotations

import copy
import contextvars
import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from .error_codes import ToolError
from .hashing import canonical_json_sha256


_PATH_KEYS = frozenset(
    {
        "path",
        "requested",
        "raw_path",
        "source_path",
        "evidence",
        "runtime",
        "pptx",
        "spec",
        "build_report",
        "structure_report",
        "render_report",
        "fontconfig_path",
        "report",
        "overlay",
        "diff",
        "asset_path",
        "font_path",
        "tool_path",
    }
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class PathComponentBinding:
    lexical_path: Path
    lexical_stat: tuple[int, int, int]
    target_identity: tuple[int, int]
    link_bytes: bytes | None


@dataclass(frozen=True)
class LexicalPathBinding:
    parents: tuple[PathComponentBinding, ...]
    leaf_stat: tuple[int, int, int, int, int, int]
    leaf_link_bytes: bytes | None
    target_stat: tuple[int, int, int, int, int, int]


@dataclass(frozen=True)
class EvidenceSnapshot:
    """Original identity plus the immutable path consumed by validators."""

    original_path: Path
    validation_path: Path
    sha256: str
    identity: tuple[int, int]
    size: int
    mtime_ns: int
    ctime_ns: int
    preserve_path_semantics: bool = False
    lexical_binding: LexicalPathBinding | None = None

    @property
    def path(self) -> Path:
        """Compatibility alias: all validators must consume this stable path."""
        return self.validation_path


class StableEvidenceView:
    """Capture files once, then validate only private materialized bytes."""

    def __init__(self) -> None:
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self.root: Path | None = None
        self._by_original: dict[Path, EvidenceSnapshot] = {}
        self._by_validation: dict[Path, EvidenceSnapshot] = {}
        self._content_paths: dict[tuple[int, int, str, int], Path] = {}

    def __enter__(self) -> StableEvidenceView:
        self._temporary = tempfile.TemporaryDirectory(prefix="review-evidence-")
        self.root = Path(self._temporary.name).resolve(strict=True)
        self.root.chmod(0o700)
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> bool:
        assert self._temporary is not None
        self._temporary.cleanup()
        self._temporary = None
        self.root = None
        self._by_original.clear()
        self._by_validation.clear()
        self._content_paths.clear()
        return False

    @staticmethod
    def _resolve(value: str | Path) -> Path:
        try:
            return Path(value).expanduser().resolve(strict=True)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise ToolError(
                "REVIEW_ADMISSION_NOT_ISSUED",
                str(value),
                "required input is missing or unstable",
            ) from exc

    @staticmethod
    def _literal_absolute(value: str | Path) -> Path:
        try:
            candidate = Path(value)
            if not candidate.is_absolute():
                raise ValueError("path is not literally absolute")
            return candidate
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise ToolError(
                "REVIEW_ADMISSION_NOT_ISSUED",
                str(value),
                "spec path must remain a literal absolute path",
            ) from exc

    @staticmethod
    def _lexical_absolute(value: str | Path) -> Path:
        try:
            candidate = Path(value).expanduser()
            if not candidate.is_absolute():
                candidate = Path.cwd() / candidate
            return candidate
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise ToolError(
                "REVIEW_ADMISSION_NOT_ISSUED",
                str(value),
                "required input path is invalid",
            ) from exc

    @staticmethod
    def _ensure_no_symlink_component(path: Path) -> None:
        try:
            if any(part.is_symlink() for part in (path, *path.parents)):
                raise OSError("path contains a symlink component")
        except (OSError, RuntimeError, ValueError) as exc:
            raise ToolError(
                "REVIEW_ADMISSION_NOT_ISSUED",
                str(path),
                "spec path must not contain symlink components",
            ) from exc

    @staticmethod
    def _read_open_file(descriptor: int, before: os.stat_result) -> bytes:
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if tuple(getattr(before, field) for field in fields) != tuple(
            getattr(after, field) for field in fields
        ):
            raise OSError("file changed while being captured")
        return b"".join(chunks)

    @staticmethod
    def _stat_binding(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
        return (
            value.st_dev,
            value.st_ino,
            value.st_mode,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )

    @staticmethod
    def _component_stat_binding(value: os.stat_result) -> tuple[int, int, int]:
        return (value.st_dev, value.st_ino, value.st_mode)

    @staticmethod
    def _link_bytes(
        name: str, *, directory_descriptor: int, value: os.stat_result
    ) -> bytes | None:
        if not stat.S_ISLNK(value.st_mode):
            return None
        return os.fsencode(os.readlink(name, dir_fd=directory_descriptor))

    @classmethod
    def _open_lexical_file(
        cls, path: Path, *, read_payload: bool
    ) -> tuple[bytes, os.stat_result, LexicalPathBinding]:
        parts = path.parts
        if not path.is_absolute() or len(parts) < 2:
            raise OSError("evidence path must be absolute")
        directory_descriptor = os.open(
            path.anchor,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        parents: list[PathComponentBinding] = []
        lexical_parent = Path(path.anchor)
        try:
            root_stat = os.fstat(directory_descriptor)
            parents.append(
                PathComponentBinding(
                    lexical_path=lexical_parent,
                    lexical_stat=cls._component_stat_binding(root_stat),
                    target_identity=(root_stat.st_dev, root_stat.st_ino),
                    link_bytes=None,
                )
            )
            for name in parts[1:-1]:
                before = os.stat(
                    name, dir_fd=directory_descriptor, follow_symlinks=False
                )
                link_bytes = cls._link_bytes(
                    name,
                    directory_descriptor=directory_descriptor,
                    value=before,
                )
                child = os.open(
                    name,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=directory_descriptor,
                )
                try:
                    target = os.fstat(child)
                    after = os.stat(
                        name,
                        dir_fd=directory_descriptor,
                        follow_symlinks=False,
                    )
                    if (
                        cls._component_stat_binding(after)
                        != cls._component_stat_binding(before)
                        or cls._link_bytes(
                            name,
                            directory_descriptor=directory_descriptor,
                            value=after,
                        )
                        != link_bytes
                    ):
                        raise OSError("parent path changed while being bound")
                except BaseException:
                    os.close(child)
                    raise
                os.close(directory_descriptor)
                directory_descriptor = child
                lexical_parent = lexical_parent / name
                parents.append(
                    PathComponentBinding(
                        lexical_path=lexical_parent,
                        lexical_stat=cls._component_stat_binding(before),
                        target_identity=(target.st_dev, target.st_ino),
                        link_bytes=link_bytes,
                    )
                )

            leaf_name = parts[-1]
            leaf_before = os.stat(
                leaf_name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            leaf_link = cls._link_bytes(
                leaf_name,
                directory_descriptor=directory_descriptor,
                value=leaf_before,
            )
            leaf_descriptor = os.open(
                leaf_name,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
                dir_fd=directory_descriptor,
            )
            try:
                target_before = os.fstat(leaf_descriptor)
                if not stat.S_ISREG(target_before.st_mode):
                    raise OSError("not a regular file")
                payload = (
                    cls._read_open_file(leaf_descriptor, target_before)
                    if read_payload
                    else b""
                )
                leaf_after = os.stat(
                    leaf_name,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
                if (
                    cls._stat_binding(leaf_after) != cls._stat_binding(leaf_before)
                    or cls._link_bytes(
                        leaf_name,
                        directory_descriptor=directory_descriptor,
                        value=leaf_after,
                    )
                    != leaf_link
                ):
                    raise OSError("leaf path changed while being bound")
            finally:
                os.close(leaf_descriptor)
            return (
                payload,
                target_before,
                LexicalPathBinding(
                    parents=tuple(parents),
                    leaf_stat=cls._stat_binding(leaf_before),
                    leaf_link_bytes=leaf_link,
                    target_stat=cls._stat_binding(target_before),
                ),
            )
        finally:
            os.close(directory_descriptor)

    @classmethod
    def _capture_lexical_file(
        cls, path: Path
    ) -> tuple[bytes, os.stat_result, LexicalPathBinding]:
        payload, target, binding = cls._open_lexical_file(
            path, read_payload=True
        )
        _empty, current_target, current_binding = cls._open_lexical_file(
            path, read_payload=False
        )
        if (
            current_binding != binding
            or (current_target.st_dev, current_target.st_ino)
            != (target.st_dev, target.st_ino)
        ):
            raise OSError("lexical path changed while being captured")
        return payload, target, binding

    def capture(
        self,
        value: str | Path,
        label: str,
        *,
        preserve_path_semantics: bool = False,
        bind_lexical_path: bool = False,
    ) -> tuple[bytes, EvidenceSnapshot]:
        if self.root is None:
            raise RuntimeError("stable evidence view is not active")
        original = (
            self._literal_absolute(value)
            if preserve_path_semantics
            else (
                self._lexical_absolute(value)
                if bind_lexical_path
                else self._resolve(value)
            )
        )
        if preserve_path_semantics:
            self._ensure_no_symlink_component(original)
        existing = self._by_original.get(original) or self._by_validation.get(original)
        if existing is not None:
            return existing.validation_path.read_bytes(), existing

        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor: int | None = None
        try:
            if preserve_path_semantics or not bind_lexical_path:
                descriptor = os.open(original, flags)
                before = os.fstat(descriptor)
                if not stat.S_ISREG(before.st_mode):
                    raise OSError("not a regular file")
                payload = self._read_open_file(descriptor, before)
                self._ensure_no_symlink_component(original)
                lexical_binding = None
            else:
                payload, before, lexical_binding = self._capture_lexical_file(
                    original
                )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise ToolError(
                "REVIEW_ADMISSION_NOT_ISSUED",
                label,
                "required input is missing or unstable",
            ) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)



        digest = hashlib.sha256(payload).hexdigest()
        suffix = original.suffix[:24]
        content_key = (before.st_dev, before.st_ino, digest, len(payload))
        validation_path = self._content_paths.get(content_key)
        if validation_path is None:
            validation_path = self.root / (
                f"{len(self._content_paths):04d}-{digest}{suffix}"
            )
            output_descriptor: int | None = None
            try:
                output_descriptor = os.open(
                    validation_path,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_CLOEXEC", 0),
                    stat.S_IMODE(before.st_mode),
                )
                view = memoryview(payload)
                while view:
                    written = os.write(output_descriptor, view)
                    if written <= 0:
                        raise OSError("short stable-evidence write")
                    view = view[written:]
                os.fsync(output_descriptor)
            except OSError as exc:
                raise ToolError(
                    "REVIEW_ADMISSION_NOT_ISSUED",
                    label,
                    "cannot materialize stable validation evidence",
                ) from exc
            finally:
                if output_descriptor is not None:
                    os.close(output_descriptor)
            self._content_paths[content_key] = validation_path

        snapshot = EvidenceSnapshot(
            original_path=original,
            validation_path=validation_path,
            sha256=digest,
            identity=(before.st_dev, before.st_ino),
            size=before.st_size,
            mtime_ns=before.st_mtime_ns,
            ctime_ns=before.st_ctime_ns,
            preserve_path_semantics=preserve_path_semantics,
            lexical_binding=lexical_binding,
        )
        self._by_original[original] = snapshot
        self._by_validation.setdefault(validation_path, snapshot)
        return payload, snapshot

    def load_json(
        self, value: str | Path, label: str
    ) -> tuple[dict[str, Any], EvidenceSnapshot]:
        raw, snapshot = self.capture(value, label)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ToolError(
                "REVIEW_ADMISSION_NOT_ISSUED",
                label,
                "input must be valid UTF-8 JSON",
            ) from exc
        if not isinstance(payload, dict):
            raise ToolError(
                "REVIEW_ADMISSION_NOT_ISSUED",
                label,
                "input JSON root must be an object",
            )
        return payload, snapshot

    def _candidate_snapshot(
        self, value: str, *, preserve_path_semantics: bool
    ) -> EvidenceSnapshot | None:
        try:
            candidate = Path(value) if preserve_path_semantics else Path(value).expanduser()
            if not candidate.is_absolute():
                return None
            if not candidate.is_file():
                return None
        except (OSError, RuntimeError, TypeError, ValueError):
            return None
        _raw, snapshot = self.capture(
            candidate,
            value,
            preserve_path_semantics=preserve_path_semantics,
            bind_lexical_path=not preserve_path_semantics,
        )
        return snapshot

    def rebind_paths(
        self, payload: Any, *, preserve_path_semantics: bool = False
    ) -> Any:
        """Deep-copy JSON and replace consumed absolute paths with stable copies."""

        def walk(value: Any, key: str | None = None) -> Any:
            if isinstance(value, dict):
                return {item_key: walk(item, item_key) for item_key, item in value.items()}
            if isinstance(value, list):
                return [walk(item, key) for item in value]
            if isinstance(value, str) and key in _PATH_KEYS:
                snapshot = self._candidate_snapshot(
                    value, preserve_path_semantics=preserve_path_semantics
                )
                if snapshot is not None:
                    return str(snapshot.validation_path)
            return copy.deepcopy(value)

        return walk(payload)

    def capture_paths(
        self, payload: Any, *, preserve_path_semantics: bool = False
    ) -> None:
        """Capture every recognized path without changing the supplied facts."""

        def walk(value: Any, key: str | None = None) -> None:
            if isinstance(value, dict):
                for item_key, item in value.items():
                    walk(item, item_key)
                return
            if isinstance(value, list):
                for item in value:
                    walk(item, key)
                return
            if isinstance(value, str) and key in _PATH_KEYS:
                self._candidate_snapshot(
                    value, preserve_path_semantics=preserve_path_semantics
                )

        walk(payload)

    def content_path(self, value: str | Path) -> Path:
        """Resolve one already-captured original path to its stable content."""
        try:
            candidate = Path(value)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise ToolError(
                "REVIEW_ADMISSION_NOT_ISSUED",
                str(value),
                "stable evidence path is invalid",
            ) from exc
        snapshot = self._by_original.get(candidate)
        if snapshot is None:
            raise ToolError(
                "REVIEW_ADMISSION_NOT_ISSUED",
                str(value),
                "production content path was not captured",
            )
        return snapshot.validation_path

    def materialize_alias_directory(
        self,
        aliases: dict[str, Path],
        label: str,
    ) -> Path:
        """Link captured stable bytes under loader-required private basenames."""

        if self.root is None:
            raise RuntimeError("stable evidence view is not active")
        directory = self.root / f"aliases-{len(self._by_validation):04d}"
        try:
            directory.mkdir(mode=0o700)
            for name, path in sorted(aliases.items()):
                if (
                    not name
                    or name in {".", ".."}
                    or Path(name).name != name
                    or "/" in name
                    or "\0" in name
                ):
                    raise OSError("alias name is not a safe basename")
                snapshot = self._by_validation.get(Path(path))
                if snapshot is None:
                    raise OSError("alias source is not captured stable evidence")
                os.link(
                    snapshot.validation_path,
                    directory / name,
                    follow_symlinks=False,
                )
            descriptor = os.open(
                directory,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise ToolError(
                "REVIEW_ADMISSION_NOT_ISSUED",
                label,
                "cannot materialize locked runtime dependencies",
            ) from exc
        return directory

    def project_json(
        self, snapshot: EvidenceSnapshot, payload: dict[str, Any], label: str
    ) -> EvidenceSnapshot:
        """Materialize a path-rebound JSON projection for producer re-reads."""
        if self.root is None:
            raise RuntimeError("stable evidence view is not active")
        try:
            encoded = (
                json.dumps(
                    payload,
                    allow_nan=False,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8")
        except (TypeError, UnicodeError, ValueError, OverflowError, RecursionError) as exc:
            raise ToolError(
                "REVIEW_ADMISSION_NOT_ISSUED",
                label,
                "cannot project stable JSON evidence",
            ) from exc
        projection_hash = hashlib.sha256(encoded).hexdigest()
        projection = self.root / f"projection-{len(self._by_validation):04d}-{projection_hash}.json"
        descriptor: int | None = None
        try:
            descriptor = os.open(
                projection,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
            view = memoryview(encoded)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short stable JSON projection write")
                view = view[written:]
            os.fsync(descriptor)
        except OSError as exc:
            raise ToolError(
                "REVIEW_ADMISSION_NOT_ISSUED",
                label,
                "cannot materialize stable JSON projection",
            ) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

        projected = EvidenceSnapshot(
            original_path=snapshot.original_path,
            validation_path=projection,
            sha256=snapshot.sha256,
            identity=snapshot.identity,
            size=snapshot.size,
            mtime_ns=snapshot.mtime_ns,
            ctime_ns=snapshot.ctime_ns,
            preserve_path_semantics=snapshot.preserve_path_semantics,
            lexical_binding=snapshot.lexical_binding,
        )
        self._by_original[snapshot.original_path] = projected
        self._by_validation[projection] = projected
        return projected

    @property
    def original_snapshots(self) -> tuple[EvidenceSnapshot, ...]:
        """Return every captured original path exactly once."""
        return tuple(self._by_original.values())

    def ensure_originals_current(self, snapshots: list[EvidenceSnapshot]) -> None:
        """Recheck original path identity and bytes after stable validation."""
        seen: set[Path] = set()
        for snapshot in snapshots:
            if snapshot.original_path in seen:
                continue
            seen.add(snapshot.original_path)
            current_binding: LexicalPathBinding | None = None
            if snapshot.lexical_binding is not None:
                try:
                    raw, current_stat, current_binding = self._capture_lexical_file(
                        snapshot.original_path
                    )
                    current = (current_stat.st_dev, current_stat.st_ino)
                except (OSError, RuntimeError, TypeError, ValueError) as exc:
                    raise ToolError(
                        "REVIEW_ADMISSION_NOT_ISSUED",
                        str(snapshot.original_path),
                        "evidence disappeared before publication",
                    ) from exc
            else:
                raw, current = self._read_current(
                    snapshot.original_path,
                    preserve_path_semantics=snapshot.preserve_path_semantics,
                )
            if (
                current != snapshot.identity
                or hashlib.sha256(raw).hexdigest() != snapshot.sha256
                or current_binding != snapshot.lexical_binding
            ):
                raise ToolError(
                    "REVIEW_ADMISSION_NOT_ISSUED",
                    str(snapshot.original_path),
                    "evidence changed before publication",
                )

    @staticmethod
    def _read_current(
        path: Path, *, preserve_path_semantics: bool = False
    ) -> tuple[bytes, tuple[int, int]]:
        descriptor: int | None = None
        try:
            if preserve_path_semantics:
                StableEvidenceView._ensure_no_symlink_component(path)
            descriptor = os.open(
                path,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise OSError("not a regular file")
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            after = os.fstat(descriptor)
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ):
                raise OSError("file changed while being rechecked")
            if preserve_path_semantics:
                StableEvidenceView._ensure_no_symlink_component(path)
            return b"".join(chunks), (before.st_dev, before.st_ino)
        except (OSError, ToolError) as exc:
            raise ToolError(
                "REVIEW_ADMISSION_NOT_ISSUED",
                str(path),
                "evidence disappeared before publication",
            ) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

_ACTIVE_VIEW: contextvars.ContextVar[StableEvidenceView | None] = (
    contextvars.ContextVar("review_evidence_view", default=None)
)


class _EvidenceViewActivation:
    def __init__(self, view: StableEvidenceView) -> None:
        self.view = view
        self.token: contextvars.Token[StableEvidenceView | None] | None = None

    def __enter__(self) -> None:
        self.token = _ACTIVE_VIEW.set(self.view)

    def __exit__(self, _type: object, _value: object, _traceback: object) -> bool:
        assert self.token is not None
        _ACTIVE_VIEW.reset(self.token)
        self.token = None
        return False


def activate_evidence_view(view: StableEvidenceView) -> _EvidenceViewActivation:
    """Make one private stable view available to all evidence consumers."""

    return _EvidenceViewActivation(view)


def current_evidence_view() -> StableEvidenceView | None:
    return _ACTIVE_VIEW.get()


def stable_content_path(value: str | Path) -> Path:
    """Return stable bytes for content reads while preserving caller path facts."""
    view = current_evidence_view()
    if view is None:
        return Path(value)
    return view.content_path(value)


def error(code: str, path: str, detail: str) -> ToolError:
    return ToolError(code, path, detail)


def is_sha256(value: Any) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def snapshot_file(value: str | Path, label: str) -> tuple[bytes, EvidenceSnapshot]:
    view = current_evidence_view()
    if view is not None:
        return view.capture(value, label)
    try:
        path = Path(value).expanduser().resolve(strict=True)
        if not path.is_file():
            raise OSError("not a regular file")
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            payload = stream.read()
            after = os.fstat(stream.fileno())
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if tuple(getattr(before, field) for field in fields) != tuple(
            getattr(after, field) for field in fields
        ):
            raise OSError("file changed while being read")
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise error(
            "REVIEW_ADMISSION_NOT_ISSUED",
            label,
            "required input is missing or unstable",
        ) from exc
    return payload, EvidenceSnapshot(
        original_path=path,
        validation_path=path,
        sha256=hashlib.sha256(payload).hexdigest(),
        identity=(before.st_dev, before.st_ino),
        size=before.st_size,
        mtime_ns=before.st_mtime_ns,
        ctime_ns=before.st_ctime_ns,
    )


def load_json(value: str | Path, label: str) -> tuple[dict[str, Any], EvidenceSnapshot]:
    view = current_evidence_view()
    if view is not None:
        payload, snapshot = view.load_json(value, label)
    else:
        raw, snapshot = snapshot_file(value, label)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise error(
                "REVIEW_ADMISSION_NOT_ISSUED",
                label,
                "input must be valid UTF-8 JSON",
            ) from exc
        if not isinstance(payload, dict):
            raise error(
                "REVIEW_ADMISSION_NOT_ISSUED",
                label,
                "input JSON root must be an object",
            )
    try:
        canonical_json_sha256(payload)
    except (TypeError, UnicodeError, ValueError, OverflowError, RecursionError) as exc:
        raise error(
            "REVIEW_ADMISSION_NOT_ISSUED",
            label,
            "input must contain finite JSON values",
        ) from exc
    return payload, snapshot


def expect(condition: bool, path: str, detail: str) -> None:
    if not condition:
        raise error("REVIEW_ADMISSION_NOT_ISSUED", path, detail)


def identity(snapshot: EvidenceSnapshot) -> dict[str, str]:
    return {"path": str(snapshot.original_path), "sha256": snapshot.sha256}


def recorded_file(
    value: Any,
    label: str,
    snapshots: list[EvidenceSnapshot],
    *,
    expected_path: Path | None = None,
    image: bool = False,
) -> EvidenceSnapshot:
    expect(isinstance(value, dict), label, "file identity must be an object")
    raw_path = value.get("path")
    digest = value.get("sha256")
    expect(
        isinstance(raw_path, str) and bool(raw_path),
        f"{label}.path",
        "path is required",
    )
    expect(is_sha256(digest), f"{label}.sha256", "lowercase SHA-256 is required")
    _raw, snapshot = snapshot_file(raw_path, f"{label}.path")
    expect(snapshot.sha256 == digest, f"{label}.sha256", "reported file hash is stale")
    if expected_path is not None:
        expect(snapshot.path == expected_path, f"{label}.path", "reported path is stale")
    if image:
        try:
            with Image.open(snapshot.path) as opened:
                opened.load()
                width, height = opened.size
            expect(width > 0 and height > 0, label, "image dimensions must be positive")
        except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
            raise error(
                "REVIEW_ADMISSION_NOT_ISSUED",
                label,
                "evidence must be a decodable image",
            ) from exc
    snapshots.append(snapshot)
    return snapshot


def ensure_unchanged(snapshots: list[EvidenceSnapshot]) -> None:
    view = current_evidence_view()
    if view is not None:
        view.ensure_originals_current(snapshots)
        return
    seen: set[Path] = set()
    for snapshot in snapshots:
        if snapshot.original_path in seen:
            continue
        seen.add(snapshot.original_path)
        binding: LexicalPathBinding | None = None
        if snapshot.lexical_binding is not None:
            try:
                raw, current, binding = StableEvidenceView._capture_lexical_file(
                    snapshot.original_path
                )
                identity = (current.st_dev, current.st_ino)
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                raise error(
                    "REVIEW_ADMISSION_NOT_ISSUED",
                    str(snapshot.original_path),
                    "evidence disappeared before publication",
                ) from exc
        else:
            raw, identity = StableEvidenceView._read_current(
                snapshot.original_path,
                preserve_path_semantics=snapshot.preserve_path_semantics,
            )
        digest = hashlib.sha256(raw).hexdigest()
        expect(
            identity == snapshot.identity
            and digest == snapshot.sha256
            and binding == snapshot.lexical_binding,
            str(snapshot.original_path),
            "evidence changed before publication",
        )
