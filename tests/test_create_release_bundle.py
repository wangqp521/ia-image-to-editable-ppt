from __future__ import annotations

import json
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from scripts.lib.error_codes import ToolError
from scripts.lib.hashing import file_sha256

try:
    from scripts import create_release_bundle as release_bundle
except ImportError:
    release_bundle = None


def fixed_metadata() -> dict[str, object]:
    return {
        "git_commit": "a" * 40,
        "capability_manifest_sha256": "b" * 64,
        "test_count": 751,
        "canary_commit": "c" * 40,
    }


def create_release_bundle(
    source: Path,
    destination: Path,
    metadata: dict[str, object],
) -> dict[str, object]:
    if release_bundle is None:
        raise AssertionError("release bundle tool is not implemented")
    return release_bundle.create_release_bundle(source, destination, metadata)


class ReleaseBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.destination = self.root / "release"
        self.source.mkdir()
        (self.source / "SKILL.md").write_text(
            "---\nname: test\ndescription: test\n---\n",
            encoding="utf-8",
        )
        nested = self.source / "scripts" / "nested"
        nested.mkdir(parents=True)
        (nested / "tool.py").write_text("VALUE = 1\n", encoding="utf-8")
        for excluded in (
            "docs",
            ".git",
            ".worktrees",
            ".canary",
            ".release-staging",
            ".superpowers",
        ):
            folder = self.source / excluded
            folder.mkdir()
            (folder / "excluded.txt").write_text("excluded", encoding="utf-8")
        cache = nested / "__pycache__"
        cache.mkdir()
        (cache / "tool.cpython-313.pyc").write_bytes(b"cache")
        (nested / "orphan.pyc").write_bytes(b"cache")

    def test_bundle_excludes_development_files_and_hashes_payload(self) -> None:
        manifest = create_release_bundle(
            self.source, self.destination, fixed_metadata()
        )

        self.assertTrue((self.destination / "SKILL.md").is_file())
        for excluded in (
            "docs",
            ".git",
            ".worktrees",
            ".canary",
            ".release-staging",
            ".superpowers",
        ):
            self.assertFalse((self.destination / excluded).exists())
        self.assertFalse(
            (self.destination / "scripts" / "nested" / "__pycache__").exists()
        )
        self.assertFalse(
            (self.destination / "scripts" / "nested" / "orphan.pyc").exists()
        )
        self.assertEqual(
            file_sha256(self.destination / "SKILL.md"),
            manifest["files"]["SKILL.md"],
        )
        self.assertEqual(
            [
                "SKILL.md",
                "scripts/nested/tool.py",
            ],
            list(manifest["files"]),
        )

    def test_manifest_is_canonical_and_contains_release_identity(self) -> None:
        metadata = fixed_metadata()
        first = create_release_bundle(self.source, self.destination, metadata)
        second_destination = self.destination.with_name("release-2")
        second = create_release_bundle(self.source, second_destination, metadata)

        self.assertEqual(first, second)
        self.assertEqual({key: first[key] for key in metadata}, metadata)
        self.assertNotIn("timestamp", first)
        self.assertNotIn("release-manifest.json", first["files"])
        expected_bytes = json.dumps(
            first,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        self.assertEqual(
            expected_bytes,
            (self.destination / "release-manifest.json").read_bytes(),
        )
        self.assertNotIn(
            os.fsencode(str(self.source)),
            (self.destination / "release-manifest.json").read_bytes(),
        )

    def test_existing_destination_is_not_overwritten(self) -> None:
        self.destination.mkdir()
        marker = self.destination / "owner.txt"
        marker.write_text("existing", encoding="utf-8")

        with self.assertRaises(ToolError) as raised:
            create_release_bundle(
                self.source, self.destination, fixed_metadata()
            )

        self.assertEqual("RELEASE_DESTINATION_EXISTS", raised.exception.code)
        self.assertEqual("existing", marker.read_text(encoding="utf-8"))

    def test_symlink_source_is_rejected_without_publishing_destination(self) -> None:
        (self.source / "unsafe").symlink_to(self.source / "SKILL.md")

        with self.assertRaises(ToolError) as raised:
            create_release_bundle(
                self.source, self.destination, fixed_metadata()
            )

        self.assertEqual("RELEASE_SOURCE_UNSAFE", raised.exception.code)
        self.assertFalse(self.destination.exists())

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO requires os.mkfifo")
    def test_special_source_is_rejected_without_publishing_destination(self) -> None:
        os.mkfifo(self.source / "unsafe.fifo")

        with self.assertRaises(ToolError) as raised:
            create_release_bundle(
                self.source, self.destination, fixed_metadata()
            )

        self.assertEqual("RELEASE_SOURCE_UNSAFE", raised.exception.code)
        self.assertFalse(self.destination.exists())

    def test_special_node_in_excluded_tree_is_not_silently_skipped(self) -> None:
        (self.source / ".canary" / "unsafe").symlink_to(
            self.source / "SKILL.md"
        )

        with self.assertRaises(ToolError) as raised:
            create_release_bundle(
                self.source, self.destination, fixed_metadata()
            )

        self.assertEqual("RELEASE_SOURCE_UNSAFE", raised.exception.code)
        self.assertFalse(self.destination.exists())

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO requires os.mkfifo")
    def test_fifo_in_excluded_tree_is_not_silently_skipped(self) -> None:
        os.mkfifo(self.source / ".superpowers" / "unsafe.fifo")

        with self.assertRaises(ToolError) as raised:
            create_release_bundle(
                self.source, self.destination, fixed_metadata()
            )

        self.assertEqual("RELEASE_SOURCE_UNSAFE", raised.exception.code)
        self.assertFalse(self.destination.exists())

    def test_directory_swap_to_symlink_cannot_redirect_copy(self) -> None:
        included = self.source / "scripts" / "nested"
        saved = self.source / "scripts" / "saved"
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "tool.py").write_text("MALICIOUS = True\n", encoding="utf-8")
        real_collect = release_bundle._collect_source

        def swap_after_collection(*args: object, **kwargs: object):
            collected = real_collect(*args, **kwargs)
            included.rename(saved)
            included.symlink_to(outside, target_is_directory=True)
            return collected

        with mock.patch.object(
            release_bundle,
            "_collect_source",
            side_effect=swap_after_collection,
        ):
            with self.assertRaises(ToolError) as raised:
                create_release_bundle(
                    self.source, self.destination, fixed_metadata()
                )

        self.assertEqual("RELEASE_SOURCE_UNSAFE", raised.exception.code)
        self.assertFalse(self.destination.exists())

    def test_directory_swap_to_regular_directory_cannot_redirect_copy(self) -> None:
        included = self.source / "scripts" / "nested"
        saved = self.source / "scripts" / "saved"
        replacement = self.root / "replacement"
        replacement.mkdir()
        (replacement / "tool.py").write_text(
            "MALICIOUS = True\n", encoding="utf-8"
        )
        real_collect = release_bundle._collect_source

        def swap_after_collection(*args: object, **kwargs: object):
            collected = real_collect(*args, **kwargs)
            included.rename(saved)
            replacement.rename(included)
            return collected

        with mock.patch.object(
            release_bundle,
            "_collect_source",
            side_effect=swap_after_collection,
        ):
            with self.assertRaises(ToolError) as raised:
                create_release_bundle(
                    self.source, self.destination, fixed_metadata()
                )

        self.assertEqual("RELEASE_SOURCE_UNSAFE", raised.exception.code)
        self.assertFalse(self.destination.exists())

    def test_source_root_identity_drift_cannot_publish(self) -> None:
        saved = self.root / "saved-source"
        real_collect = release_bundle._collect_source

        def replace_root_after_collection(*args: object, **kwargs: object):
            collected = real_collect(*args, **kwargs)
            self.source.rename(saved)
            shutil.copytree(saved, self.source)
            return collected

        with mock.patch.object(
            release_bundle,
            "_collect_source",
            side_effect=replace_root_after_collection,
        ):
            with self.assertRaises(ToolError) as raised:
                create_release_bundle(
                    self.source, self.destination, fixed_metadata()
                )

        self.assertEqual("RELEASE_SOURCE_UNSAFE", raised.exception.code)
        self.assertFalse(self.destination.exists())

    def test_publish_race_does_not_overwrite_competing_destination(self) -> None:
        if release_bundle is None:
            self.fail("release bundle tool is not implemented")

        def competing_publish(
            _source_parent_fd: int,
            _source_name: str,
            destination_parent_fd: int,
            destination_name: str,
        ) -> None:
            os.mkdir(destination_name, mode=0o700, dir_fd=destination_parent_fd)
            destination_fd = os.open(
                destination_name,
                os.O_RDONLY | os.O_DIRECTORY,
                dir_fd=destination_parent_fd,
            )
            try:
                marker_fd = os.open(
                    "owner.txt",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=destination_fd,
                )
                try:
                    os.write(marker_fd, b"competitor")
                finally:
                    os.close(marker_fd)
            finally:
                os.close(destination_fd)
            raise FileExistsError(destination_name)

        with mock.patch.object(
            release_bundle,
            "_rename_no_replace_at",
            create=True,
            side_effect=competing_publish,
        ):
            with self.assertRaises(ToolError) as raised:
                create_release_bundle(
                    self.source, self.destination, fixed_metadata()
                )

        self.assertEqual("RELEASE_DESTINATION_EXISTS", raised.exception.code)
        self.assertEqual(
            "competitor",
            (self.destination / "owner.txt").read_text(encoding="utf-8"),
        )
        self.assertEqual(
            ["release", "source"],
            sorted(path.name for path in self.root.iterdir()),
        )

    def test_path_based_rename_hook_is_not_used_for_publication(self) -> None:
        path_hook_called = False

        def forbidden_path_publish(*_args: object, **_kwargs: object) -> None:
            nonlocal path_hook_called
            path_hook_called = True
            raise FileExistsError("legacy path publication was called")

        with mock.patch.object(
            release_bundle,
            "rename_no_replace",
            create=True,
            side_effect=forbidden_path_publish,
        ):
            try:
                create_release_bundle(
                    self.source, self.destination, fixed_metadata()
                )
            except ToolError:
                pass

        self.assertFalse(path_hook_called)

    def test_parent_path_swap_cannot_redirect_destination(self) -> None:
        parent = self.root / "secure-parent"
        parent.mkdir(mode=0o700)
        os.chmod(parent, 0o700)
        destination = parent / "release"
        saved_parent = self.root / "saved-parent"
        real_publish = getattr(release_bundle, "_rename_no_replace_at", None)
        publish_calls = 0

        def swap_parent_then_publish(
            source_parent_fd: int,
            source_name: str,
            destination_parent_fd: int,
            destination_name: str,
        ) -> None:
            nonlocal publish_calls
            publish_calls += 1
            parent.rename(saved_parent)
            parent.mkdir(mode=0o700)
            os.chmod(parent, 0o700)
            if real_publish is None:
                raise AssertionError("dirfd no-replace publication is not implemented")
            real_publish(
                source_parent_fd,
                source_name,
                destination_parent_fd,
                destination_name,
            )

        with mock.patch.object(
            release_bundle,
            "_rename_no_replace_at",
            create=True,
            side_effect=swap_parent_then_publish,
        ):
            create_release_bundle(
                self.source, destination, fixed_metadata()
            )

        self.assertEqual(1, publish_calls)
        self.assertTrue((saved_parent / "release" / "SKILL.md").is_file())
        self.assertFalse((parent / "release").exists())

    def test_insecure_destination_parent_is_rejected(self) -> None:
        parent = self.root / "insecure-parent"
        parent.mkdir()
        os.chmod(parent, 0o777)
        destination = parent / "release"

        with self.assertRaises(ToolError) as raised:
            create_release_bundle(
                self.source, destination, fixed_metadata()
            )

        self.assertEqual("RELEASE_DESTINATION_UNSAFE", raised.exception.code)
        self.assertFalse(destination.exists())

    def test_cleanup_identity_mismatch_preserves_competitor_and_is_explicit(
        self,
    ) -> None:
        competitor_envelope: list[Path] = []
        saved_envelope: list[Path] = []

        def replace_envelope_then_fail(
            _source_parent_fd: int,
            _source_name: str,
            _destination_parent_fd: int,
            _destination_name: str,
        ) -> None:
            envelopes = [
                path
                for path in self.root.iterdir()
                if path.is_dir()
                and path.name.startswith(f".{self.destination.name}.")
                and path.name.endswith(".txn")
            ]
            self.assertEqual(1, len(envelopes))
            envelope = envelopes[0]
            saved = envelope.with_name(f"{envelope.name}.saved")
            envelope.rename(saved)
            envelope.mkdir(mode=0o700)
            os.chmod(envelope, 0o700)
            (envelope / "owner.txt").write_text(
                "competitor", encoding="utf-8"
            )
            competitor_envelope.append(envelope)
            saved_envelope.append(saved)
            raise OSError("publish interrupted")

        with mock.patch.object(
            release_bundle,
            "_rename_no_replace_at",
            create=True,
            side_effect=replace_envelope_then_fail,
        ):
            with self.assertRaises(ToolError) as raised:
                create_release_bundle(
                    self.source, self.destination, fixed_metadata()
                )

        self.assertEqual("RELEASE_CLEANUP_INCOMPLETE", raised.exception.code)
        self.assertEqual(1, len(competitor_envelope))
        self.assertEqual(
            "competitor",
            (competitor_envelope[0] / "owner.txt").read_text(encoding="utf-8"),
        )
        self.assertEqual(1, len(saved_envelope))
        self.assertIn(saved_envelope[0].name, raised.exception.detail)

    def test_real_dirfd_no_replace_publishes_without_overwrite(self) -> None:
        self.assertTrue(
            hasattr(release_bundle, "_rename_no_replace_at"),
            "dirfd no-replace publication is not implemented",
        )
        parent_fd = os.open(
            self.root,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        try:
            os.mkdir("primitive-source", mode=0o700, dir_fd=parent_fd)
            source_fd = os.open(
                "primitive-source",
                os.O_RDONLY | os.O_DIRECTORY,
                dir_fd=parent_fd,
            )
            try:
                marker_fd = os.open(
                    "payload",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=source_fd,
                )
                os.write(marker_fd, b"owned")
                os.close(marker_fd)
            finally:
                os.close(source_fd)
            try:
                release_bundle._rename_no_replace_at(
                    parent_fd,
                    "primitive-source",
                    parent_fd,
                    "primitive-destination",
                )
            except NotImplementedError as exc:
                self.skipTest(str(exc))
            self.assertFalse((self.root / "primitive-source").exists())
            self.assertEqual(
                "owned",
                (self.root / "primitive-destination" / "payload").read_text(
                    encoding="utf-8"
                ),
            )

            os.mkdir("competitor", mode=0o700, dir_fd=parent_fd)
            with self.assertRaises(FileExistsError):
                release_bundle._rename_no_replace_at(
                    parent_fd,
                    "competitor",
                    parent_fd,
                    "primitive-destination",
                )
            self.assertTrue((self.root / "competitor").is_dir())
            self.assertEqual(
                "owned",
                (self.root / "primitive-destination" / "payload").read_text(
                    encoding="utf-8"
                ),
            )
        finally:
            os.close(parent_fd)

    def test_metadata_must_be_exact_release_identity(self) -> None:
        metadata = fixed_metadata()
        metadata["timestamp"] = "2026-07-31T00:00:00Z"

        with self.assertRaises(ToolError) as raised:
            create_release_bundle(self.source, self.destination, metadata)

        self.assertEqual("RELEASE_METADATA_INVALID", raised.exception.code)
        self.assertFalse(self.destination.exists())


class ReleaseBundleCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.destination = self.root / "release"
        self.source.mkdir()
        self._write("SKILL.md", "---\nname: fixture\ndescription: fixture\n---\n")
        self._write("scripts/lib/__init__.py", "")
        self._write(
            "scripts/lib/capabilities.py",
            "def capability_manifest_sha256():\n    return 'd' * 64\n",
        )
        self._write(
            "tests/test_smoke.py",
            "import unittest\n\n"
            "class SmokeTests(unittest.TestCase):\n"
            "    def test_smoke(self):\n"
            "        self.assertTrue(True)\n",
        )
        self._write("docs/canary.md", "canary\n")
        self._write(".superpowers/private.txt", "development\n")
        self._write(
            ".gitignore",
            ".canary/\n.release-staging/\n__pycache__/\n*.py[cod]\n*.local\n",
        )
        self._git("init")
        self._git("config", "user.name", "Release Test")
        self._git("config", "user.email", "release@example.invalid")
        self._git("add", ".")
        self._git("commit", "-m", "fixture")
        self.commit = self._git("rev-parse", "HEAD").stdout.strip()

    def _write(self, relative: str, value: str) -> Path:
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
        return path

    def _git(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.source), *arguments],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def _main(self) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = release_bundle.main(
                [
                    "--source",
                    str(self.source),
                    "--destination",
                    str(self.destination),
                    "--canary-report",
                    "docs/canary.md",
                ]
            )
        return status, stdout.getvalue(), stderr.getvalue()

    def test_dirty_tracked_source_fails_before_tests_or_publication(self) -> None:
        self._write("SKILL.md", "dirty\n")

        with mock.patch.object(
            release_bundle,
            "_run_test_suite",
            return_value=1,
        ) as run_tests:
            status, _, stderr = self._main()

        self.assertEqual(2, status)
        self.assertIn("RELEASE_SOURCE_DIRTY", stderr)
        self.assertFalse(self.destination.exists())
        run_tests.assert_not_called()

    def test_staged_source_fails_before_tests_or_publication(self) -> None:
        self._write("SKILL.md", "staged\n")
        self._git("add", "SKILL.md")

        with mock.patch.object(
            release_bundle,
            "_run_test_suite",
            return_value=1,
        ) as run_tests:
            status, _, stderr = self._main()

        self.assertEqual(2, status)
        self.assertIn("RELEASE_SOURCE_DIRTY", stderr)
        self.assertFalse(self.destination.exists())
        run_tests.assert_not_called()

    def test_included_untracked_file_fails_before_tests_or_publication(self) -> None:
        self._write("included.txt", "untracked\n")

        with mock.patch.object(
            release_bundle,
            "_run_test_suite",
            return_value=1,
        ) as run_tests:
            status, _, stderr = self._main()

        self.assertEqual(2, status)
        self.assertIn("RELEASE_SOURCE_DIRTY", stderr)
        self.assertFalse(self.destination.exists())
        run_tests.assert_not_called()

    def test_included_ignored_file_fails_before_tests_or_publication(self) -> None:
        self._write("included.local", "ignored but bundle-visible\n")

        with mock.patch.object(
            release_bundle,
            "_run_test_suite",
            return_value=1,
        ) as run_tests:
            status, _, stderr = self._main()

        self.assertEqual(2, status)
        self.assertIn("RELEASE_SOURCE_DIRTY", stderr)
        self.assertFalse(self.destination.exists())
        run_tests.assert_not_called()

    def test_excluded_and_ignored_untracked_files_do_not_block(self) -> None:
        self._write("docs/untracked.txt", "excluded\n")
        self._write(".superpowers/untracked.txt", "excluded\n")
        self._write(".canary/ignored.txt", "ignored\n")

        with mock.patch.object(
            release_bundle,
            "_run_test_suite",
            return_value=1,
        ):
            status, _, stderr = self._main()

        self.assertEqual("", stderr)
        self.assertEqual(0, status)
        self.assertTrue(self.destination.is_dir())
        self.assertFalse((self.destination / "docs").exists())
        self.assertFalse((self.destination / ".superpowers").exists())

    def test_capability_identity_comes_from_source_commit(self) -> None:
        with mock.patch.object(
            release_bundle,
            "_run_test_suite",
            return_value=1,
        ):
            metadata = release_bundle._release_metadata(
                self.source, Path("docs/canary.md")
            )

        self.assertEqual("d" * 64, metadata["capability_manifest_sha256"])
        self.assertEqual(self.commit, metadata["git_commit"])
        self.assertEqual(self.commit, metadata["canary_commit"])

    def test_test_count_supports_nonpackage_tests_directory(self) -> None:
        self.assertEqual(1, release_bundle._run_test_suite(self.source))

    def test_tests_run_from_fixed_snapshot_not_mutable_worktree(self) -> None:
        committed_skill = (self.source / "SKILL.md").read_text(encoding="utf-8")
        real_capability = release_bundle._source_capability_manifest_sha256

        def mutate_after_fixed_tree_capture(guard: object) -> str:
            value = real_capability(guard)
            self._write("SKILL.md", "transient dirty worktree\n")
            return value

        def inspect_test_root(test_root: Path) -> int:
            try:
                self.assertNotEqual(self.source, test_root)
                self.assertEqual(
                    committed_skill,
                    (test_root / "SKILL.md").read_text(encoding="utf-8"),
                )
                return 1
            finally:
                self._write("SKILL.md", committed_skill)

        with (
            mock.patch.object(
                release_bundle,
                "_source_capability_manifest_sha256",
                side_effect=mutate_after_fixed_tree_capture,
            ),
            mock.patch.object(
                release_bundle,
                "_run_test_suite",
                side_effect=inspect_test_root,
            ),
        ):
            metadata = release_bundle._release_metadata(
                self.source, Path("docs/canary.md")
            )

        self.assertEqual(1, metadata["test_count"])

    def test_head_drift_during_build_cannot_publish(self) -> None:
        real_hashes = release_bundle._payload_hashes
        changed = False

        def change_head_during_build(*args: object, **kwargs: object):
            nonlocal changed
            payload = real_hashes(*args, **kwargs)
            if not changed:
                changed = True
                self._write("docs/canary.md", "new committed canary\n")
                self._git("add", "docs/canary.md")
                self._git("commit", "-m", "drift")
            return payload

        with (
            mock.patch.object(release_bundle, "_run_test_suite", return_value=1),
            mock.patch.object(
                release_bundle,
                "_payload_hashes",
                side_effect=change_head_during_build,
            ),
        ):
            status, _, stderr = self._main()

        self.assertEqual(2, status)
        self.assertIn("RELEASE_SOURCE_CHANGED", stderr)
        self.assertFalse(self.destination.exists())


if __name__ == "__main__":
    unittest.main()
