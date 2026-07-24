from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "render_preview.py"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_module():
    spec = importlib.util.spec_from_file_location("render_preview", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def write_minimal_pdf(path: Path) -> None:
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 960 540] /Contents 4 0 R >>",
        b"<< /Length 0 >>\nstream\n\nendstream",
    ]
    payload = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, body in enumerate(objects, start=1):
        offsets.append(len(payload))
        payload.extend(f"{index} 0 obj\n".encode())
        payload.extend(body)
        payload.extend(b"\nendobj\n")
    xref = len(payload)
    payload.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    payload.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        payload.extend(f"{offset:010d} 00000 n \n".encode())
    payload.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n"
        ).encode()
    )
    path.write_bytes(payload)


class RenderPreviewTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.pptx = self.root / "page.pptx"
        self.pptx.write_bytes(b"immutable-pptx")
        self.pdf_fixture = self.root / "fixture.pdf"
        write_minimal_pdf(self.pdf_fixture)
        self.png_fixture = self.root / "fixture.png"
        self.write_preview((1920, 1080), blank=False)
        self.fontconfig = self.root / "fontconfig.xml"
        self.fontconfig.write_text("<fontconfig/>", encoding="utf-8")
        self.output = self.root / "output"
        self.runtime = self.root / "runtime.json"
        self.write_tools(mutate_pptx=False)
        self.write_runtime()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_preview(self, size: tuple[int, int], *, blank: bool) -> None:
        image = Image.new("RGB", size, "white")
        if not blank:
            draw = ImageDraw.Draw(image)
            draw.rectangle((100, 100, 500, 300), fill="black")
        image.save(self.png_fixture)

    def write_executable(self, name: str, source: str) -> Path:
        path = self.root / name
        path.write_text("#!/usr/bin/env python3\n" + source, encoding="utf-8")
        path.chmod(0o755)
        return path

    def write_tools(self, *, mutate_pptx: bool) -> None:
        mutation = (
            "Path(sys.argv[-1]).write_bytes(b'changed-pptx')\n"
            if mutate_pptx
            else ""
        )
        self.soffice = self.write_executable(
            "soffice",
            "import shutil, sys\n"
            "from pathlib import Path\n"
            f"fixture = Path({str(self.pdf_fixture)!r})\n"
            "outdir = Path(sys.argv[sys.argv.index('--outdir') + 1])\n"
            "source = Path(sys.argv[-1])\n"
            "outdir.mkdir(parents=True, exist_ok=True)\n"
            "shutil.copy2(fixture, outdir / (source.stem + '.pdf'))\n"
            + mutation,
        )
        self.pdffonts = self.write_executable(
            "pdffonts",
            "print('name type encoding emb sub uni object ID')\n"
            "print('----------------------------------------')\n"
            "print('AAAAAA+NotoSansCJKsc-Regular Type1 Builtin yes yes yes 1 0')\n",
        )
        self.pdftoppm = self.write_executable(
            "pdftoppm",
            "import shutil, sys\n"
            "from pathlib import Path\n"
            f"fixture = Path({str(self.png_fixture)!r})\n"
            "prefix = Path(sys.argv[-1])\n"
            "shutil.copy2(fixture, prefix.with_suffix('.png'))\n",
        )

    def write_flaky_soffice(self, returncodes: list[int]) -> tuple[Path, Path]:
        attempts_path = self.root / "soffice-attempts.txt"
        invocations_path = self.root / "soffice-invocations.txt"
        self.soffice = self.write_executable(
            "soffice",
            "import os, shutil, signal, sys\n"
            "from pathlib import Path\n"
            f"fixture = Path({str(self.pdf_fixture)!r})\n"
            f"attempts_path = Path({str(attempts_path)!r})\n"
            f"invocations_path = Path({str(invocations_path)!r})\n"
            f"returncodes = {returncodes!r}\n"
            "attempt = int(attempts_path.read_text()) + 1 if attempts_path.exists() else 1\n"
            "attempts_path.write_text(str(attempt))\n"
            "profile = next(arg for arg in sys.argv if arg.startswith('-env:UserInstallation='))\n"
            "outdir = Path(sys.argv[sys.argv.index('--outdir') + 1])\n"
            "with invocations_path.open('a', encoding='utf-8') as stream:\n"
            "    stream.write(f'{profile}\\t{outdir}\\n')\n"
            "returncode = returncodes[min(attempt - 1, len(returncodes) - 1)]\n"
            "if returncode == -6:\n"
            "    os.kill(os.getpid(), signal.SIGABRT)\n"
            "if returncode:\n"
            "    raise SystemExit(returncode)\n"
            "source = Path(sys.argv[-1])\n"
            "outdir.mkdir(parents=True, exist_ok=True)\n"
            "shutil.copy2(fixture, outdir / (source.stem + '.pdf'))\n",
        )
        self.write_runtime()
        return attempts_path, invocations_path

    def write_runtime(self) -> None:
        payload = {
            "valid": True,
            "errors": [],
            "renderer_backend": "libreoffice",
            "preview_size": [1920, 1080],
            "executables": {
                "soffice": {
                    "path": str(self.soffice),
                    "version": "LibreOffice 26.2.3.2",
                    "sha256": sha256(self.soffice),
                },
                "pdftoppm": {
                    "path": str(self.pdftoppm),
                    "version": "pdftoppm 26.07.0",
                    "sha256": sha256(self.pdftoppm),
                },
                "pdffonts": {
                    "path": str(self.pdffonts),
                    "version": "pdffonts 26.07.0",
                    "sha256": sha256(self.pdffonts),
                },
            },
            "fontconfig": {
                "path": str(self.fontconfig),
                "sha256": sha256(self.fontconfig),
            },
        }
        self.runtime.write_text(json.dumps(payload), encoding="utf-8")

    def test_script_exists(self) -> None:
        self.assertTrue(SCRIPT.is_file())

    @unittest.skipUnless(SCRIPT.is_file(), "render_preview.py not implemented")
    def test_render_preview_writes_atomic_report(self) -> None:
        module = load_module()
        report = module.render_preview(self.pptx, self.output, self.runtime)

        self.assertEqual(sha256(self.pptx), report["pptx"]["sha256"])
        self.assertEqual([1920, 1080], report["preview"]["size"])
        self.assertEqual("libreoffice", report["renderer"]["backend"])
        self.assertEqual(
            ["NotoSansCJKsc-Regular"],
            report["font_report"]["resolved_fonts"],
        )
        self.assertTrue((self.output / "render-report.json").is_file())
        self.assertTrue((self.output / "current-preview.png").is_file())
        self.assertTrue((self.output / "page.pdf").is_file())

    @unittest.skipUnless(SCRIPT.is_file(), "render_preview.py not implemented")
    def test_render_preview_rejects_nonempty_output(self) -> None:
        module = load_module()
        self.output.mkdir()
        (self.output / "old.txt").write_text("old", encoding="utf-8")

        with self.assertRaises(module.RenderError) as caught:
            module.render_preview(self.pptx, self.output, self.runtime)

        self.assertEqual("RENDER_OUTPUT_NOT_EMPTY", caught.exception.code)

    @unittest.skipUnless(SCRIPT.is_file(), "render_preview.py not implemented")
    def test_render_preview_rejects_changed_input_hash(self) -> None:
        module = load_module()
        self.write_tools(mutate_pptx=True)
        self.write_runtime()

        with self.assertRaises(module.RenderError) as caught:
            module.render_preview(self.pptx, self.output, self.runtime)

        self.assertEqual("RENDER_INPUT_CHANGED", caught.exception.code)
        self.assertFalse(self.output.exists())

    @unittest.skipUnless(SCRIPT.is_file(), "render_preview.py not implemented")
    def test_render_preview_rejects_wrong_png_size(self) -> None:
        module = load_module()
        self.write_preview((1600, 900), blank=False)

        with self.assertRaises(module.RenderError) as caught:
            module.render_preview(self.pptx, self.output, self.runtime)

        self.assertEqual("RENDER_PREVIEW_SIZE_MISMATCH", caught.exception.code)

    @unittest.skipUnless(SCRIPT.is_file(), "render_preview.py not implemented")
    def test_render_preview_rejects_blank_png(self) -> None:
        module = load_module()
        self.write_preview((1920, 1080), blank=True)

        with self.assertRaises(module.RenderError) as caught:
            module.render_preview(self.pptx, self.output, self.runtime)

        self.assertEqual("RENDER_PREVIEW_INVALID", caught.exception.code)

    @unittest.skipUnless(SCRIPT.is_file(), "render_preview.py not implemented")
    def test_render_preview_recovers_once_from_macos_sigabrt(self) -> None:
        module = load_module()
        attempts_path, invocations_path = self.write_flaky_soffice([-6, 0])

        with mock.patch.object(module.sys, "platform", "darwin"):
            report = module.render_preview(self.pptx, self.output, self.runtime)

        self.assertEqual("2", attempts_path.read_text())
        invocations = invocations_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(2, len(invocations))
        self.assertNotEqual(invocations[0].split("\t")[1], invocations[1].split("\t")[1])
        self.assertEqual(2, report["renderer"]["attempt_count"])
        self.assertEqual("SIGABRT", report["renderer"]["recovered_from"])

    @unittest.skipUnless(SCRIPT.is_file(), "render_preview.py not implemented")
    def test_render_preview_stops_after_second_macos_sigabrt(self) -> None:
        module = load_module()
        attempts_path, _ = self.write_flaky_soffice([-6, -6])

        with mock.patch.object(module.sys, "platform", "darwin"):
            with self.assertRaises(module.RenderError) as caught:
                module.render_preview(self.pptx, self.output, self.runtime)

        self.assertEqual(
            "RENDER_MACOS_APPLICATION_REGISTRATION_FAILED",
            caught.exception.code,
        )
        self.assertEqual(-6, caught.exception.returncode)
        self.assertEqual("2", attempts_path.read_text())
        self.assertFalse(self.output.exists())

    @unittest.skipUnless(SCRIPT.is_file(), "render_preview.py not implemented")
    def test_render_preview_does_not_retry_non_sigabrt(self) -> None:
        module = load_module()
        attempts_path, _ = self.write_flaky_soffice([7, 0])

        with mock.patch.object(module.sys, "platform", "darwin"):
            with self.assertRaises(module.RenderError) as caught:
                module.render_preview(self.pptx, self.output, self.runtime)

        self.assertEqual(7, caught.exception.returncode)
        self.assertEqual("1", attempts_path.read_text())
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
