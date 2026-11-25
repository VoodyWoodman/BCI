from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from create_release_manifest import create_manifest


class ReleaseManifestTests(unittest.TestCase):
    def test_creates_manifest_and_checksums_for_preview_release(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            installer = root / "BCI_Setup_0.5.2-alpha_x64.exe"
            portable = root / "BCI_Portable_0.5.2-alpha.zip"
            installer.write_bytes(b"installer")
            portable.write_bytes(b"portable")
            output = root / "out"

            manifest = create_manifest("0.5.2-alpha", installer, portable, output)
            self.assertEqual(manifest["repository"], "VoodyWoodman/BCI")
            self.assertEqual(manifest["channel"], "preview")
            self.assertEqual(manifest["assets"]["installer"]["sha256"], hashlib.sha256(b"installer").hexdigest())
            parsed = json.loads((output / "release-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(parsed["version"], "0.5.2-alpha")
            sums = (output / "SHA256SUMS.txt").read_text(encoding="utf-8")
            self.assertIn("BCI_Setup_0.5.2-alpha_x64.exe", sums)
            self.assertIn("BCI_Portable_0.5.2-alpha.zip", sums)

    def test_stable_release_uses_stable_channel(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            installer = root / "BCI_Setup_1.0.0_x64.exe"
            portable = root / "BCI_Portable_1.0.0.zip"
            installer.write_bytes(b"setup")
            portable.write_bytes(b"zip")
            manifest = create_manifest("1.0.0", installer, portable, root / "out")
            self.assertEqual(manifest["channel"], "stable")

    def test_requires_existing_assets(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaises(FileNotFoundError):
                create_manifest("0.5.2-alpha", root / "missing.exe", root / "missing.zip", root / "out")

    def test_rejects_unsafe_version(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            installer = root / "BCI_Setup_0.5.2-alpha_x64.exe"
            portable = root / "BCI_Portable_0.5.2-alpha.zip"
            installer.write_bytes(b"setup")
            portable.write_bytes(b"zip")
            with self.assertRaises(ValueError):
                create_manifest("../0.5.2", installer, portable, root / "out")


if __name__ == "__main__":
    unittest.main()
