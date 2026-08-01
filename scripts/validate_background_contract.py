#!/usr/bin/env python3
"""Validate and atomically publish postbuild background evidence."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import stat
import sys
from pathlib import Path
from typing import Any


SCRIPTS_ROOT = Path(__file__).resolve().parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from lib.background_contracts import validate_background_postbuild
from lib.error_codes import ToolError


def _publish_json_no_overwrite(path: Path, payload: dict[str, Any]) -> None:
    """Publish one durable JSON report without replacing an existing path."""
    destination = path.expanduser()
    parent = destination.parent
    if destination.is_symlink():
        raise ToolError(
            "BUILD_OUTPUT_INCOMPLETE",
            str(destination),
            "output path already exists",
        )
    if parent.is_symlink():
        raise ToolError(
            "BUILD_OUTPUT_INCOMPLETE",
            str(parent),
            "output parent must be an existing real directory",
        )
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

    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    directory_fd: int | None = None
    try:
        directory_fd = os.open(parent, directory_flags)
        if not stat.S_ISDIR(os.fstat(directory_fd).st_mode):
            raise OSError("output parent is not a directory")
    except OSError as exc:
        if directory_fd is not None:
            try:
                os.close(directory_fd)
            except OSError:
                pass
        raise ToolError(
            "BUILD_OUTPUT_INCOMPLETE",
            str(parent),
            "output parent must be an existing real directory",
        ) from exc

    temporary_name: str | None = None
    temporary_fd: int | None = None
    link_succeeded = False
    committed = False
    pending_error: ToolError | None = None
    pending_cause: BaseException | None = None
    preserved_destination_detail = (
        "destination visible/preserved; durability or ownership uncertain"
    )

    def unlink_name(name: str) -> None:
        os.unlink(name, dir_fd=directory_fd)

    def fsync_directory() -> None:
        os.fsync(directory_fd)

    def cleanup_uncommitted_temp() -> None:
        nonlocal temporary_name
        if temporary_name is None:
            return
        unlink_name(temporary_name)
        temporary_name = None
        fsync_directory()

    def uncommitted_failure_detail(
        primary_error: BaseException,
        cleanup_error: OSError,
        temp_name: str,
    ) -> str:
        primary_cause = primary_error.__cause__ or primary_error
        if temporary_name is None:
            residue = (
                f"temporary {temp_name} was unlinked, but cleanup durability is "
                "unconfirmed"
            )
        else:
            residue = f"temporary residue {temp_name} may remain"
        return (
            f"publication failed: {primary_cause}; cleanup failed: "
            f"{cleanup_error}; {residue}"
        )

    def cleanup_directory_fsync_detail() -> str:
        try:
            fsync_directory()
        except OSError as fsync_error:
            return (
                f"cleanup directory fsync failed: {fsync_error}; cleanup "
                "durability is unconfirmed"
            )
        return "cleanup directory fsync completed"

    def cleanup_post_link_temp() -> str:
        nonlocal temporary_name
        if temporary_name is not None:
            temp_name = temporary_name
            try:
                unlink_name(temp_name)
            except OSError as unlink_error:
                temp_detail = (
                    f"temporary residue {temp_name} may remain: cleanup unlink "
                    f"failed: {unlink_error}"
                )
            else:
                temporary_name = None
                temp_detail = "temporary name removed"
        else:
            temp_detail = "temporary name already removed"
        return f"{temp_detail}; {cleanup_directory_fsync_detail()}"

    def post_link_failure_detail(primary_detail: str) -> str:
        return (
            f"{primary_detail}; {preserved_destination_detail}; "
            f"{cleanup_post_link_temp()}; caller must inspect destination"
        )

    try:
        try:
            os.stat(destination.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileExistsError as exc:
            raise ToolError(
                "BUILD_OUTPUT_INCOMPLETE",
                str(destination),
                "output path already exists",
            ) from exc
        except FileNotFoundError:
            pass
        else:
            raise ToolError(
                "BUILD_OUTPUT_INCOMPLETE",
                str(destination),
                "output path already exists",
            )

        create_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        create_flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        for _attempt in range(32):
            candidate = f".{destination.name}.{secrets.token_hex(8)}.tmp"
            try:
                temporary_fd = os.open(
                    candidate,
                    create_flags,
                    0o600,
                    dir_fd=directory_fd,
                )
            except FileExistsError:
                continue
            temporary_name = candidate
            break
        if temporary_fd is None or temporary_name is None:
            raise ToolError(
                "BUILD_OUTPUT_INCOMPLETE",
                str(destination),
                "cannot allocate a unique temporary output",
            )

        try:
            view = memoryview(encoded)
            while view:
                written = os.write(temporary_fd, view)
                if written <= 0:
                    raise OSError("short temporary output write")
                view = view[written:]
            os.fsync(temporary_fd)
            temporary_stat = os.fstat(temporary_fd)
            if not stat.S_ISREG(temporary_stat.st_mode):
                raise OSError("temporary output is not a regular file")
        finally:
            os.close(temporary_fd)
            temporary_fd = None

        def destination_entry_status() -> tuple[str, str]:
            try:
                current = os.stat(
                    destination.name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return (
                    "missing",
                    "last destination observation found no destination entry",
                )
            except OSError as status_error:
                return (
                    "unknown",
                    "last destination observation could not verify ownership: "
                    f"{status_error}",
                )
            if (
                stat.S_ISREG(current.st_mode)
                and (current.st_dev, current.st_ino)
                == (temporary_stat.st_dev, temporary_stat.st_ino)
            ):
                return (
                    "owned",
                    "last destination observation matched this publication",
                )
            return (
                "competing",
                "last destination observation found a competing destination",
            )

        try:
            os.link(
                temporary_name,
                destination.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
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
        link_succeeded = True

        destination_status, destination_detail = destination_entry_status()
        if destination_status != "owned":
            raise ToolError(
                "BUILD_OUTPUT_INCOMPLETE",
                str(destination),
                f"published output identity changed; {destination_detail}",
            )

        try:
            fsync_directory()
        except OSError as exc:
            _destination_status, destination_detail = destination_entry_status()
            raise ToolError(
                "BUILD_OUTPUT_INCOMPLETE",
                str(destination),
                f"cannot durably publish output: {exc}; {destination_detail}",
            ) from exc

        destination_status, destination_detail = destination_entry_status()
        if destination_status != "owned":
            raise ToolError(
                "BUILD_OUTPUT_INCOMPLETE",
                str(destination),
                "published output changed after directory fsync; "
                f"{destination_detail}",
            )

        committed = True
        try:
            unlink_name(temporary_name)
            temporary_name = None
        except OSError as exc:
            raise ToolError(
                "BUILD_OUTPUT_INCOMPLETE",
                str(destination),
                f"temporary residue {temporary_name} may remain: cleanup unlink "
                f"failed: {exc}; {preserved_destination_detail}; "
                f"{cleanup_directory_fsync_detail()}; "
                "caller must inspect destination",
            ) from exc
        try:
            fsync_directory()
        except OSError as exc:
            raise ToolError(
                "BUILD_OUTPUT_INCOMPLETE",
                str(destination),
                f"cleanup directory fsync failed: {exc}; "
                f"{preserved_destination_detail}; temporary name removed; cleanup "
                "durability is unconfirmed; caller must inspect destination",
            ) from exc
    except ToolError as primary_error:
        if link_succeeded and not committed:
            pending_error = ToolError(
                primary_error.code,
                primary_error.path,
                post_link_failure_detail(primary_error.detail),
                primary_error.capability,
            )
            pending_cause = primary_error
        elif not link_succeeded and not committed and temporary_name is not None:
            temp_name = temporary_name
            try:
                cleanup_uncommitted_temp()
            except OSError as cleanup_error:
                pending_error = ToolError(
                    "BUILD_OUTPUT_INCOMPLETE",
                    str(destination),
                    uncommitted_failure_detail(
                        primary_error, cleanup_error, temp_name
                    ),
                )
                pending_cause = cleanup_error
            else:
                pending_error = ToolError(
                    primary_error.code,
                    primary_error.path,
                    primary_error.detail,
                    primary_error.capability,
                )
                pending_cause = primary_error
        else:
            pending_error = ToolError(
                primary_error.code,
                primary_error.path,
                primary_error.detail,
                primary_error.capability,
            )
            pending_cause = primary_error
    except OSError as primary_error:
        if link_succeeded and not committed:
            pending_error = ToolError(
                "BUILD_OUTPUT_INCOMPLETE",
                str(destination),
                post_link_failure_detail(
                    f"cannot publish output without overwrite: {primary_error}"
                ),
            )
            pending_cause = primary_error
        elif temporary_name is not None and not committed:
            temp_name = temporary_name
            try:
                cleanup_uncommitted_temp()
            except OSError as cleanup_error:
                pending_error = ToolError(
                    "BUILD_OUTPUT_INCOMPLETE",
                    str(destination),
                    uncommitted_failure_detail(
                        primary_error, cleanup_error, temp_name
                    ),
                )
                pending_cause = cleanup_error
            else:
                pending_error = ToolError(
                    "BUILD_OUTPUT_INCOMPLETE",
                    str(destination),
                    f"cannot publish output without overwrite: {primary_error}",
                )
                pending_cause = primary_error
        else:
            pending_error = ToolError(
                "BUILD_OUTPUT_INCOMPLETE",
                str(destination),
                f"cannot publish output without overwrite: {primary_error}",
            )
            pending_cause = primary_error
    finally:
        if temporary_fd is not None:
            try:
                os.close(temporary_fd)
            except OSError:
                pass
        try:
            os.close(directory_fd)
        except OSError as close_error:
            close_detail = (
                "output directory descriptor could not be closed cleanly: "
                f"{close_error}"
            )
            if pending_error is None:
                if link_succeeded:
                    close_detail = (
                        f"{close_detail}; {preserved_destination_detail}; caller "
                        "must inspect destination"
                    )
                pending_error = ToolError(
                    "BUILD_OUTPUT_INCOMPLETE",
                    str(destination),
                    close_detail,
                )
                pending_cause = close_error
            else:
                primary_diagnostic = pending_error
                pending_error = ToolError(
                    primary_diagnostic.code,
                    primary_diagnostic.path,
                    f"{primary_diagnostic.detail}; {close_detail}",
                    primary_diagnostic.capability,
                )

    if pending_error is not None:
        if pending_cause is None:
            raise pending_error
        raise pending_error from pending_cause


def _emit_error(error: ToolError) -> None:
    print(json.dumps(error.as_dict(), ensure_ascii=False), file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path)
    parser.add_argument("--pptx", type=Path, required=True)
    parser.add_argument("--build-report", type=Path, required=True)
    parser.add_argument("--structure-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.output.exists() or args.output.is_symlink():
        _emit_error(
            ToolError(
                "BUILD_OUTPUT_INCOMPLETE",
                str(args.output.expanduser()),
                "output path already exists",
            )
        )
        return 2

    report = validate_background_postbuild(
        args.spec,
        args.pptx,
        args.build_report,
        args.structure_report,
    )
    try:
        _publish_json_no_overwrite(args.output, report)
    except ToolError as exc:
        _emit_error(exc)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
