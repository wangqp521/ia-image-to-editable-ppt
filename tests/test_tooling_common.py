from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from lib.atomic_write import atomic_write_json
from lib.error_codes import ToolError
from lib.geometry import map_xywh_to_slide
from lib.hashing import canonical_json_sha256
from lib.schema_io import load_schema_v2


class ToolingCommonTests(unittest.TestCase):
    def test_map_xywh_uses_page_frame_and_emu_rounding(self) -> None:
        canvas = {
            "page_frame_bbox": [100, 50, 1600, 900],
            "slide_size_emu": [12192000, 6858000],
        }
        self.assertEqual(
            map_xywh_to_slide([100, 50, 800, 450], canvas),
            [0, 0, 6096000, 3429000],
        )

    def test_mapping_rejects_bbox_outside_page_frame(self) -> None:
        canvas = {
            "page_frame_bbox": [100, 50, 1600, 900],
            "slide_size_emu": [12192000, 6858000],
        }
        with self.assertRaisesRegex(ToolError, "BBOX_OUT_OF_RANGE"):
            map_xywh_to_slide([99, 50, 10, 10], canvas)

    def test_load_schema_v2_rejects_other_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "spec.json"
            path.write_text('{"schema_version": 3}', encoding="utf-8")
            with self.assertRaisesRegex(ToolError, "SPEC_SCHEMA_VERSION_UNSUPPORTED"):
                load_schema_v2(path)

    def test_canonical_hash_ignores_object_key_order(self) -> None:
        self.assertEqual(
            canonical_json_sha256({"b": 2, "a": 1}),
            canonical_json_sha256({"a": 1, "b": 2}),
        )

    def test_atomic_json_write_is_canonical_and_readable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "report.json"
            atomic_write_json(path, {"b": 2, "a": 1})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"a": 1, "b": 2})
            self.assertTrue(path.read_text(encoding="utf-8").endswith("\n"))


if __name__ == "__main__":
    unittest.main()
