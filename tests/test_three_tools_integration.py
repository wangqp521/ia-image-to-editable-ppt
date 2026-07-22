from __future__ import annotations

import copy
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

from tests.fixture_specs import add_picture_asset, make_text_spec


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import build_pptx_from_spec
import create_asset_crop_review
import extract_assets
import scaffold_reconstruction


SPEC_VALIDATOR_SPEC = importlib.util.spec_from_file_location(
    "integration_spec_validator", SCRIPTS / "validate_reconstruction_spec.py"
)
PPTX_VALIDATOR_SPEC = importlib.util.spec_from_file_location(
    "integration_pptx_validator", SCRIPTS / "validate_pptx.py"
)
if SPEC_VALIDATOR_SPEC is None or SPEC_VALIDATOR_SPEC.loader is None:
    raise RuntimeError("cannot load spec validator")
if PPTX_VALIDATOR_SPEC is None or PPTX_VALIDATOR_SPEC.loader is None:
    raise RuntimeError("cannot load PPTX validator")
SPEC_VALIDATOR = importlib.util.module_from_spec(SPEC_VALIDATOR_SPEC)
SPEC_VALIDATOR_SPEC.loader.exec_module(SPEC_VALIDATOR)
PPTX_VALIDATOR = importlib.util.module_from_spec(PPTX_VALIDATOR_SPEC)
PPTX_VALIDATOR_SPEC.loader.exec_module(PPTX_VALIDATOR)


class ThreeToolsIntegrationTests(unittest.TestCase):
    def _run_pipeline(self, root: Path) -> tuple[dict, dict, dict, dict]:
        spec = add_picture_asset(make_text_spec(root))
        del spec["elements"][0]["slide_bbox"]
        scaffolded, scaffold_report = scaffold_reconstruction.scaffold_spec(spec, {"valid": True})
        extracted, asset_report = extract_assets.extract_assets(scaffolded, root / "assets")
        review = create_asset_crop_review.render_asset_review(extracted, root / "asset-review.png")
        prebuild = SPEC_VALIDATOR.validate_spec(
            extracted,
            stage="prebuild",
            asset_review_report=review,
            require_asset_review=True,
        )
        self.assertTrue(prebuild["valid"], prebuild["errors"])
        pptx = root / "page.pptx"
        build_report = build_pptx_from_spec.build_single_page(extracted, prebuild, pptx)
        validation = PPTX_VALIDATOR.validate_pptx(
            pptx,
            reconstruction_spec=extracted,
            build_report=build_report,
        )
        self.assertTrue(validation["valid"], validation)
        return extracted, scaffold_report, asset_report, build_report

    def test_prebuild_rejects_stale_asset_review_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = add_picture_asset(make_text_spec(root))
            extracted, _ = extract_assets.extract_assets(spec, root / "assets")
            review = create_asset_crop_review.render_asset_review(extracted, root / "asset-review.png")
            review["spec_sha256"] = "0" * 64
            report = SPEC_VALIDATOR.validate_spec(
                extracted,
                stage="prebuild",
                asset_review_report=review,
                require_asset_review=True,
            )
            self.assertFalse(report["valid"])
            self.assertIn("SPEC_ASSET_REVIEW_SPEC_HASH_MISMATCH", {item["code"] for item in report["errors"]})

    def test_full_pipeline_builds_native_text_and_independent_picture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spec, _, _, build = self._run_pipeline(Path(directory))
            self.assertEqual(build["elements"]["title"]["object_type"], "text")
            self.assertEqual(build["elements"]["photo"]["object_type"], "picture")
            self.assertEqual(spec["schema_version"], 2)

    def test_repeated_runs_keep_asset_and_object_manifest_stable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, _, first_assets, first_build = self._run_pipeline(root / "first")
            second, _, second_assets, second_build = self._run_pipeline(root / "second")
            self.assertEqual(
                [item["asset_sha256"] for item in first_assets["items"]],
                [item["asset_sha256"] for item in second_assets["items"]],
            )
            self.assertEqual(
                {key: value["ooxml_names"] for key, value in first_build["elements"].items()},
                {key: value["ooxml_names"] for key, value in second_build["elements"].items()},
            )
            self.assertEqual(first["reading_order"], second["reading_order"])

    def test_text_change_does_not_change_existing_asset_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            extracted, _, first_assets, _ = self._run_pipeline(root)
            changed = copy.deepcopy(extracted)
            changed["elements"][0]["content"]["text"] = "新标题"
            changed["modules"]["typography"]["items"][0]["text"] = "新标题"
            changed["modules"]["typography"]["items"][0]["runs"][0]["end"] = 3
            changed["modules"]["typography"]["items"][0]["paragraphs"][0]["end"] = 3
            second, second_report = extract_assets.extract_assets(changed, root / "assets")
            self.assertEqual(
                first_assets["items"][0]["asset_sha256"],
                second_report["items"][0]["asset_sha256"],
            )
            self.assertEqual(second["elements"][0]["content"]["text"], "新标题")


if __name__ == "__main__":
    unittest.main()
