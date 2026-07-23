from pathlib import Path
import unittest
import xml.etree.ElementTree as ET


SKILL_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = SKILL_ROOT / "assets" / "fontconfig-macos.conf"
SKILL_PATH = SKILL_ROOT / "SKILL.md"
RENDER_PATH = SKILL_ROOT / "scripts" / "render_preview.py"


class MacOSFontconfigTests(unittest.TestCase):
    def test_config_includes_required_macos_font_directories(self):
        self.assertTrue(CONFIG_PATH.is_file(), "缺少 macOS fontconfig 配置资产")
        root = ET.parse(CONFIG_PATH).getroot()
        font_dirs = {node.text for node in root.findall("dir")}

        self.assertIn("/System/Library/Fonts", font_dirs)
        self.assertIn("/System/Library/Fonts/Supplemental", font_dirs)
        self.assertIn("/Library/Fonts", font_dirs)
        self.assertIn("~/Library/Fonts", font_dirs)
        self.assertIn(
            "/System/Library/AssetsV2/com_apple_MobileAsset_Font8",
            font_dirs,
        )

    def test_skill_uses_config_for_macos_soffice_preview(self):
        skill_text = SKILL_PATH.read_text(encoding="utf-8")
        render_text = RENDER_PATH.read_text(encoding="utf-8")

        self.assertIn("--fontconfig assets/fontconfig-macos.conf", skill_text)
        self.assertIn("render_preview.py", skill_text)
        self.assertIn('env["FONTCONFIG_FILE"]', render_text)
        self.assertIn('"--headless"', render_text)
        self.assertIn('"--convert-to"', render_text)
        self.assertIn('"pdf"', render_text)

    def test_skill_uses_isolated_libreoffice_user_installation(self):
        skill_text = SKILL_PATH.read_text(encoding="utf-8")
        render_text = RENDER_PATH.read_text(encoding="utf-8")

        self.assertIn("render_preview.py", skill_text)
        self.assertIn('f"-env:UserInstallation={profile.resolve().as_uri()}"', render_text)
        self.assertIn("tempfile.mkdtemp", render_text)
        self.assertIn("RENDER_PDF_INVALID", render_text)


if __name__ == "__main__":
    unittest.main()
