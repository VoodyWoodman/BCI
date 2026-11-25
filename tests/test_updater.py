from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import Mock

from bci_updater import (
    GitHubUpdateClient,
    UpdateError,
    UpdateSettings,
    compare_versions,
    has_installer_marker,
    is_check_due,
    load_update_settings,
    save_update_settings,
)


REPO = "VoodyWoodman/BCI"
TAG = "v0.5.3-alpha"
INSTALLER_NAME = "BCI_Setup_0.5.3-alpha_x64.exe"
INSTALLER_BYTES = b"test installer payload, not an executable"
INSTALLER_SHA = hashlib.sha256(INSTALLER_BYTES).hexdigest()
MANIFEST_URL = f"https://github.com/{REPO}/releases/download/{TAG}/release-manifest.json"
INSTALLER_URL = f"https://github.com/{REPO}/releases/download/{TAG}/{INSTALLER_NAME}"


class FakeResponse:
    def __init__(self, payload: bytes, url: str, headers: dict[str, str] | None = None) -> None:
        self._stream = io.BytesIO(payload)
        self._url = url
        self.headers = headers or {"Content-Length": str(len(payload))}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self._stream.close()

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def geturl(self) -> str:
        return self._url


def make_release_payload(*, with_manifest: bool = True, channel: str = "preview"):
    api_url = f"https://api.github.com/repos/{REPO}/releases?per_page=100"
    manifest = {
        "schema_version": 1,
        "product": "Bannerlord Compatibility Intelligence",
        "repository": REPO,
        "version": "0.5.3-alpha",
        "channel": channel,
        "architecture": "x64",
        "assets": {
            "installer": {
                "name": INSTALLER_NAME,
                "sha256": INSTALLER_SHA,
                "size_bytes": len(INSTALLER_BYTES),
            }
        },
    }
    manifest_bytes = json.dumps(manifest).encode("utf-8")
    assets = [{
        "name": INSTALLER_NAME,
        "size": len(INSTALLER_BYTES),
        "browser_download_url": INSTALLER_URL,
    }]
    if with_manifest:
        assets.append({
            "name": "release-manifest.json",
            "size": len(manifest_bytes),
            "browser_download_url": MANIFEST_URL,
        })
    release = {
        "tag_name": TAG,
        "draft": False,
        "prerelease": True,
        "html_url": f"https://github.com/{REPO}/releases/tag/{TAG}",
        "published_at": "2026-11-24T10:00:00Z",
        "body": "## Исправления\n- Проверка обновлений",
        "assets": assets,
    }
    responses = {
        api_url: FakeResponse(json.dumps([release]).encode("utf-8"), api_url),
        MANIFEST_URL: FakeResponse(manifest_bytes, MANIFEST_URL),
        INSTALLER_URL: FakeResponse(
            INSTALLER_BYTES,
            "https://release-assets.githubusercontent.com/github-production-release-asset/test",
        ),
    }
    return responses


class UpdaterVersionTests(unittest.TestCase):
    def test_semver_prerelease_and_stable_order(self):
        self.assertLess(compare_versions("0.5.2-alpha", "0.5.2"), 0)
        self.assertLess(compare_versions("0.5.2-beta.2", "0.5.2-rc.1"), 0)
        self.assertLess(compare_versions("0.5.2-alpha.1", "0.5.2-alpha.2"), 0)
        self.assertGreater(compare_versions("0.5.3-alpha", "0.5.2"), 0)
        self.assertEqual(compare_versions("v1.2.3", "1.2.3"), 0)

    def test_rejects_non_project_version_format(self):
        with self.assertRaises(ValueError):
            compare_versions("0.5", "0.5.1")


class UpdaterSettingsTests(unittest.TestCase):
    def test_save_and_load_update_preferences(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "update_settings.json"
            settings = UpdateSettings(
                channel="Stable", auto_check=False, last_check_utc="2026-11-24T10:00:00+00:00"
            )
            save_update_settings(path, settings)
            loaded = load_update_settings(path)
            self.assertEqual(loaded, settings)

    def test_corrupt_settings_fall_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "update_settings.json"
            path.write_text("{bad", encoding="utf-8")
            self.assertEqual(load_update_settings(path, default_channel="Preview"), UpdateSettings())

    def test_check_due_after_24_hours(self):
        self.assertTrue(is_check_due("2026-11-23T09:00:00+00:00"))
        self.assertFalse(is_check_due(
            "2099-11-23T09:00:00+00:00",
            now=datetime(2099, 11, 23, 10, 0, tzinfo=timezone.utc),
        ))
        self.assertTrue(is_check_due("not-a-date"))


class InstallerMarkerTests(unittest.TestCase):
    def test_windows_installer_shell_execute_uses_pointer_sized_result(self):
        source = (Path(__file__).resolve().parents[1] / "bci_updater.py").read_text(encoding="utf-8")
        self.assertIn("shell_execute.restype = ctypes.c_void_p", source)
        self.assertIn("shell_execute.argtypes", source)
        self.assertIn("result is None or result <= 32", source)

    def test_marker_detects_inno_installed_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            executable = root / "BCI.exe"
            executable.write_bytes(b"placeholder")
            self.assertFalse(has_installer_marker(executable))
            (root / ".bci-installed").write_text("Installed by BCI Setup", encoding="utf-8")
            self.assertTrue(has_installer_marker(executable))


class GitHubUpdateClientTests(unittest.TestCase):
    def test_preview_channel_loads_manifest_and_marks_update_available(self):
        responses = make_release_payload()
        client = GitHubUpdateClient(opener=lambda request, timeout: responses[request.full_url])
        result = client.check_for_updates("0.5.2-alpha", "Preview")
        self.assertTrue(result.update_available)
        self.assertEqual(result.latest_version, "0.5.3-alpha")
        self.assertTrue(result.installer_ready)
        self.assertEqual(result.installer_sha256, INSTALLER_SHA)
        self.assertEqual(result.channel, "Preview")

    def test_stable_channel_ignores_prerelease_releases(self):
        api_url = f"https://api.github.com/repos/{REPO}/releases?per_page=100"
        payload = [{
            "tag_name": "v0.9.0-alpha", "draft": False, "prerelease": True,
            "assets": [], "html_url": f"https://github.com/{REPO}/releases/tag/v0.9.0-alpha",
        }]
        client = GitHubUpdateClient(opener=lambda request, timeout: FakeResponse(json.dumps(payload).encode(), api_url))
        result = client.check_for_updates("0.5.2-alpha", "Stable")
        self.assertIsNone(result.latest_version)
        self.assertFalse(result.update_available)

    def test_preview_channel_can_select_newer_stable_release(self):
        api_url = f"https://api.github.com/repos/{REPO}/releases?per_page=100"
        payload = [
            {"tag_name": "v0.5.4-alpha", "draft": False, "prerelease": True, "assets": []},
            {"tag_name": "v0.5.5", "draft": False, "prerelease": False, "assets": []},
        ]
        client = GitHubUpdateClient(opener=lambda request, timeout: FakeResponse(json.dumps(payload).encode(), api_url))
        result = client.check_for_updates("0.5.2-alpha", "Preview")
        self.assertEqual(result.latest_version, "0.5.5")
        self.assertTrue(result.update_available)

    def test_manifest_can_require_a_minimum_updater_version(self):
        responses = make_release_payload()
        api_url = f"https://api.github.com/repos/{REPO}/releases?per_page=100"
        manifest = {
            "schema_version": 1,
            "product": "Bannerlord Compatibility Intelligence",
            "repository": REPO,
            "version": "0.5.3-alpha",
            "channel": "preview",
            "architecture": "x64",
            "minimum_updater_version": "0.5.3-alpha",
            "assets": {"installer": {
                "name": INSTALLER_NAME,
                "sha256": INSTALLER_SHA,
                "size_bytes": len(INSTALLER_BYTES),
            }},
        }
        responses[MANIFEST_URL] = FakeResponse(json.dumps(manifest).encode(), MANIFEST_URL)
        client = GitHubUpdateClient(opener=lambda request, timeout: responses[request.full_url])
        result = client.check_for_updates("0.5.2-alpha", "Preview")
        self.assertTrue(result.update_available)
        self.assertFalse(result.installer_ready)
        self.assertIn("вручную", result.error)

    def test_release_without_manifest_cannot_be_installed_automatically(self):
        responses = make_release_payload(with_manifest=False)
        client = GitHubUpdateClient(opener=lambda request, timeout: responses[request.full_url])
        result = client.check_for_updates("0.5.2-alpha", "Preview")
        self.assertTrue(result.update_available)
        self.assertFalse(result.installer_ready)
        self.assertIn("release-manifest.json", result.error)

    def test_manifest_rejects_non_x64_package(self):
        responses = make_release_payload()
        manifest_payload = json.loads(responses[MANIFEST_URL]._stream.getvalue().decode("utf-8"))
        manifest_payload["architecture"] = "arm64"
        responses[MANIFEST_URL] = FakeResponse(
            json.dumps(manifest_payload).encode("utf-8"), MANIFEST_URL
        )
        client = GitHubUpdateClient(opener=lambda request, timeout: responses[request.full_url])
        with self.assertRaises(UpdateError):
            client.check_for_updates("0.5.2-alpha", "Preview")

    def test_manifest_rejects_other_product(self):
        responses = make_release_payload()
        manifest_payload = json.loads(responses[MANIFEST_URL]._stream.getvalue().decode("utf-8"))
        manifest_payload["product"] = "Other Product"
        responses[MANIFEST_URL] = FakeResponse(
            json.dumps(manifest_payload).encode("utf-8"), MANIFEST_URL
        )
        client = GitHubUpdateClient(opener=lambda request, timeout: responses[request.full_url])
        with self.assertRaises(UpdateError):
            client.check_for_updates("0.5.2-alpha", "Preview")

    def test_download_verifies_size_and_sha256(self):
        responses = make_release_payload()
        client = GitHubUpdateClient(opener=lambda request, timeout: responses[request.full_url])
        result = client.check_for_updates("0.5.2-alpha", "Preview")
        with tempfile.TemporaryDirectory() as temp:
            downloaded = client.download_and_verify(result, temp)
            self.assertEqual(downloaded.read_bytes(), INSTALLER_BYTES)
            self.assertEqual(downloaded.name, INSTALLER_NAME)

    def test_download_rejects_wrong_sha256_and_removes_partial_file(self):
        responses = make_release_payload()
        client = GitHubUpdateClient(opener=lambda request, timeout: responses[request.full_url])
        result = client.check_for_updates("0.5.2-alpha", "Preview")
        result = result.__class__(**{**result.__dict__, "installer_sha256": "0" * 64})
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(UpdateError):
                client.download_and_verify(result, temp)
            self.assertEqual(list(Path(temp).iterdir()), [])

    def test_rejects_asset_urls_outside_official_release_path(self):
        api_url = f"https://api.github.com/repos/{REPO}/releases?per_page=100"
        payload = [{
            "tag_name": TAG,
            "draft": False,
            "prerelease": True,
            "assets": [{"name": "release-manifest.json", "browser_download_url": "https://example.com/manifest.json"}],
        }]
        client = GitHubUpdateClient(opener=lambda request, timeout: FakeResponse(json.dumps(payload).encode(), api_url))
        with self.assertRaises(UpdateError):
            client.check_for_updates("0.5.2-alpha", "Preview")

    def test_repository_is_fixed_to_official_project(self):
        with self.assertRaises(ValueError):
            GitHubUpdateClient(repository="someone/else")


if __name__ == "__main__":
    unittest.main()
