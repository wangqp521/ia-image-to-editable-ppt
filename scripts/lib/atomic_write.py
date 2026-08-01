"""Atomic file publication helpers."""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .error_codes import ToolError
from .no_replace_transactions import (
    DirectoryLock,
    FileIdentity,
    PublicationReceipt,
    TombstoneReceipt,
    TransactionFailure,
    clear_recovery_manifest,
    encode_json,
    enforce_recovery_budget,
    prepare_recovery_candidate,
    quarantine_publication,
    rename_no_replace,
    retain_recovery_tombstone,
    verify_file_receipt,
)


_rename_no_replace = rename_no_replace


def atomic_write_bytes(path: str | Path, payload: bytes) -> None:
    """Durably publish bytes at *path* without exposing a partial file."""
    destination = Path(path)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, destination)
        temporary_name = None
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def atomic_write_json(path: str | Path, payload: Any) -> None:
    """Durably publish stable, human-readable UTF-8 JSON at *path*."""
    encoded = (
        json.dumps(payload, allow_nan=False, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    ).encode("utf-8")
    atomic_write_bytes(path, encoded)


def publish_json_no_overwrite(
    path: str | Path,
    payload: Any,
    *,
    parent_locked: bool = False,
) -> PublicationReceipt:
    """Publish one JSON file without replacing an existing destination."""
    destination = Path(path).expanduser()
    try:
        encoded = encode_json(payload)
    except (TypeError, UnicodeError, ValueError, OverflowError, RecursionError) as exc:
        raise ToolError(
            "BUILD_OUTPUT_INCOMPLETE",
            str(destination),
            "JSON payload is not finite canonical data",
        ) from exc

    parent = destination.parent
    if parent.is_symlink() or not parent.is_dir():
        raise ToolError(
            "BUILD_OUTPUT_INCOMPLETE",
            str(parent),
            "output parent must be an existing real directory",
        )
    lock = (
        contextlib.nullcontext()
        if parent_locked or DirectoryLock.held_by_current_thread(parent)
        else DirectoryLock(parent)
    )
    try:
        with lock:
            try:
                os.lstat(destination)
            except FileNotFoundError:
                pass
            else:
                exists = FileExistsError(
                    errno.EEXIST,
                    os.strerror(errno.EEXIST),
                    os.fspath(destination),
                )
                raise ToolError(
                    "BUILD_OUTPUT_INCOMPLETE",
                    str(destination),
                    "output path already exists",
                ) from exists

            try:
                enforce_recovery_budget(parent, len(encoded))
            except TransactionFailure as exc:
                raise ToolError(
                    "BUILD_OUTPUT_INCOMPLETE", str(destination), exc.detail
                ) from exc

            candidate: Path | None = None
            candidate_identity: FileIdentity | None = None
            planned_receipt: PublicationReceipt | None = None
            published = False
            phase = "candidate_create"
            try:
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    dir=parent,
                    prefix=f".{destination.name}.txn-",
                    suffix=".rollback",
                    delete=False,
                ) as temporary:
                    candidate = Path(temporary.name)
                    candidate_identity = FileIdentity.from_stat(
                        os.fstat(temporary.fileno())
                    )
                    planned_receipt = PublicationReceipt(
                        destination=destination,
                        identity=candidate_identity,
                        sha256=hashlib.sha256(encoded).hexdigest(),
                        byte_count=len(encoded),
                        encoded=encoded,
                    )
                    prepare_recovery_candidate(
                        temporary.fileno(),
                        candidate,
                        planned_receipt,
                        phase=phase,
                    )
                    phase = "candidate_write"
                    temporary.write(encoded)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                phase = "publish"
                _rename_no_replace(candidate, destination)
                published = True
                receipt = planned_receipt
                assert receipt is not None
                phase = "parent_fsync"
                _fsync_directory(parent)
                phase = "postcheck"
                verify_file_receipt(receipt)
                phase = "manifest_clear"
                clear_recovery_manifest(destination, receipt)
                _fsync_directory(parent)
                return receipt
            except FileExistsError as exc:
                tombstone = (
                    retain_recovery_tombstone(
                        candidate,
                        planned_receipt,
                        phase=phase,
                        fsync_directory=_fsync_directory,
                    )
                    if candidate is not None and planned_receipt is not None
                    else None
                )
                detail = "output path already exists"
                if tombstone is not None:
                    detail += f"; {tombstone.detail()}"
                raise ToolError(
                    "BUILD_OUTPUT_INCOMPLETE", str(destination), detail
                ) from exc
            except BaseException as exc:
                tombstone: TombstoneReceipt | None = None
                if published:
                    receipt = planned_receipt
                    assert receipt is not None
                    try:
                        tombstone = quarantine_publication(
                            receipt,
                            phase=phase,
                            fsync_directory=_fsync_directory,
                            rename=_rename_no_replace,
                        )
                    except TransactionFailure as rollback_error:
                        raise ToolError(
                            "BUILD_OUTPUT_INCOMPLETE",
                            str(destination),
                            rollback_error.detail,
                        ) from exc
                elif candidate is not None and planned_receipt is not None:
                    tombstone = retain_recovery_tombstone(
                        candidate,
                        planned_receipt,
                        phase=phase,
                        fsync_directory=_fsync_directory,
                    )
                detail = "cannot publish JSON without overwrite"
                if tombstone is not None:
                    detail += f"; {tombstone.detail()}"
                raise ToolError(
                    "BUILD_OUTPUT_INCOMPLETE", str(destination), detail
                ) from exc
    except ToolError:
        raise
    except OSError as exc:
        raise ToolError(
            "BUILD_OUTPUT_INCOMPLETE",
            str(destination),
            "cannot lock or publish JSON output directory",
        ) from exc


def _resolved(path: Path) -> Path:
    try:
        return path.resolve(strict=False)
    except OSError as exc:
        raise ToolError(
            "BUILD_OUTPUT_INCOMPLETE", str(path), "cannot resolve publication path"
        ) from exc


def _fsync_file(path: Path) -> None:
    try:
        with path.open("rb") as stream:
            os.fsync(stream.fileno())
    except OSError as exc:
        raise ToolError(
            "BUILD_OUTPUT_INCOMPLETE", str(path), "cannot fsync publication candidate"
        ) from exc


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError as exc:
        raise ToolError(
            "BUILD_OUTPUT_INCOMPLETE", str(path), "cannot fsync output directory"
        ) from exc

    failure: BaseException | None = None
    failure_cause: OSError | None = None
    try:
        os.fsync(descriptor)
    except OSError as exc:
        failure = ToolError(
            "BUILD_OUTPUT_INCOMPLETE", str(path), "cannot fsync output directory"
        )
        failure_cause = exc
    except BaseException as exc:
        failure = exc
    try:
        os.close(descriptor)
    except OSError as exc:
        if failure is None:
            failure = ToolError(
                "BUILD_OUTPUT_INCOMPLETE",
                str(path),
                "cannot close output directory",
            )
            failure_cause = exc
    if failure is not None:
        if isinstance(failure, ToolError):
            raise failure from failure_cause
        raise failure


def _publish_no_overwrite(candidate: Path, destination: Path) -> None:
    """Atomically link one durable candidate into an absent destination."""
    try:
        os.link(candidate, destination)
    except FileExistsError as exc:
        raise ToolError(
            "BUILD_OUTPUT_INCOMPLETE",
            str(destination),
            "output path already exists",
        ) from exc
    except OSError as exc:
        raise ToolError(
            "BUILD_OUTPUT_INCOMPLETE",
            str(destination),
            "cannot publish output without overwrite",
        ) from exc


def publish_pair_no_overwrite(
    pptx_candidate: str | Path,
    report_candidate: str | Path,
    output_pptx: str | Path,
    output_report: str | Path,
) -> None:
    """Publish a PPTX/report pair without overwriting or leaving a half pair."""
    pptx_candidate = Path(pptx_candidate)
    report_candidate = Path(report_candidate)
    output_pptx = Path(output_pptx)
    output_report = Path(output_report)

    candidate_paths = (_resolved(pptx_candidate), _resolved(report_candidate))
    output_paths = (_resolved(output_pptx), _resolved(output_report))
    if candidate_paths[0] == candidate_paths[1]:
        raise ToolError(
            "BUILD_OUTPUT_INCOMPLETE",
            str(pptx_candidate),
            "publication candidates must be distinct",
        )
    if output_paths[0] == output_paths[1]:
        raise ToolError(
            "BUILD_OUTPUT_INCOMPLETE",
            str(output_pptx),
            "PPTX and build report paths must be distinct",
        )
    if candidate_paths[0].parent != candidate_paths[1].parent:
        raise ToolError(
            "BUILD_OUTPUT_INCOMPLETE",
            str(report_candidate),
            "publication candidates must share one transaction directory",
        )
    if set(candidate_paths).intersection(output_paths):
        raise ToolError(
            "BUILD_OUTPUT_INCOMPLETE",
            str(output_pptx),
            "publication candidates and outputs must be distinct",
        )

    for candidate in (pptx_candidate, report_candidate):
        if candidate.is_symlink() or not candidate.is_file():
            raise ToolError(
                "BUILD_OUTPUT_INCOMPLETE",
                str(candidate),
                "publication candidate must be a regular file",
            )
    for destination in (output_pptx, output_report):
        if destination.exists() or destination.is_symlink():
            raise ToolError(
                "BUILD_OUTPUT_INCOMPLETE",
                str(destination),
                "output path already exists",
            )
        if destination.parent.is_symlink() or not destination.parent.is_dir():
            raise ToolError(
                "BUILD_OUTPUT_INCOMPLETE",
                str(destination.parent),
                "output parent must be an existing real directory",
            )

    _fsync_file(pptx_candidate)
    _fsync_file(report_candidate)
    publication_pairs = (
        (pptx_candidate, output_pptx),
        (report_candidate, output_report),
    )
    candidate_identities: dict[Path, tuple[int, int]] = {}
    try:
        for candidate, destination in publication_pairs:
            candidate_stat = candidate.stat()
            candidate_identities[destination] = (
                candidate_stat.st_dev,
                candidate_stat.st_ino,
            )
    except OSError as exc:
        raise ToolError(
            "BUILD_OUTPUT_INCOMPLETE",
            str(pptx_candidate),
            "cannot inspect publication candidate",
        ) from exc
    try:
        _publish_no_overwrite(pptx_candidate, output_pptx)
        _fsync_directory(output_pptx.parent)
        _publish_no_overwrite(report_candidate, output_report)
        _fsync_directory(output_report.parent)
    except BaseException:
        rollback_error: OSError | None = None
        rollback_parents: set[Path] = set()
        for _, destination in reversed(publication_pairs):
            try:
                destination_stat = destination.lstat()
                identity = candidate_identities[destination]
                if (destination_stat.st_dev, destination_stat.st_ino) == identity:
                    destination.unlink()
                    rollback_parents.add(destination.parent)
            except FileNotFoundError:
                continue
            except OSError as exc:
                rollback_error = rollback_error or exc
        for parent in rollback_parents:
            try:
                _fsync_directory(parent)
            except ToolError:
                pass
        if rollback_error is not None:
            raise ToolError(
                "BUILD_OUTPUT_INCOMPLETE",
                str(output_pptx),
                "publication failed and pair rollback failed",
            ) from rollback_error
        raise
