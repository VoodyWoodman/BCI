from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ReleasePipelineTests(unittest.TestCase):
    def test_app_and_installer_versions_match(self):
        core = (ROOT / "bci_core.py").read_text(encoding="utf-8")
        setup = (ROOT / "BCI_Setup.iss").read_text(encoding="utf-8")
        self.assertIn('APP_VERSION = "0.5.2-alpha"', core)
        self.assertIn('#define MyAppVersion "0.5.2-alpha"', setup)

    def test_inno_installer_keeps_app_id_and_user_data_separate(self):
        setup = (ROOT / "BCI_Setup.iss").read_text(encoding="utf-8")
        self.assertIn("AppId={{E2A0D7CE-5D44-46E7-90CB-5DBA3B8B2F41}", setup)
        self.assertIn("PrivilegesRequired=admin", setup)
        self.assertIn("UsePreviousAppDir=yes", setup)
        self.assertIn("vc_redist.x64.exe", setup)
        self.assertIn("PrepareToInstall", setup)
        self.assertIn("IsVCRedistAtLeastBundled", setup)
        self.assertIn("HKLM64", setup)
        self.assertIn("3010", setup)
        self.assertIn("Flags: dontcopy noencryption", setup)
        self.assertIn("Flags: nowait runasoriginaluser", setup)
        self.assertNotIn("postinstall", setup)
        self.assertIn("GetVersionComponents", setup)
        self.assertIn("RegQueryDWordValue", setup)
        self.assertLess(setup.index('Source: "redist\\vc_redist.x64.exe"'), setup.index('Source: "dist\\BCI\\*"'))
        self.assertIn(".bci-installed", setup)
        self.assertIn("{app}\\.bci-installed", setup)

    def test_workflow_runs_tests_and_publishes_only_for_tags(self):
        workflow = (ROOT / ".github" / "workflows" / "windows-installer.yml").read_text(encoding="utf-8")
        for text in (
            "windows-latest", "python-version: '3.12'", "python -m pytest -q",
            "python -m unittest discover", "PyInstaller", "ISCC.exe",
            "create_release_manifest.py", "release-manifest.json", "SHA256SUMS.txt",
            "softprops/action-gh-release@v2", "refs/tags/", "build/bci-update-system",
            "--version=6.7.1",
        ):
            self.assertIn(text, workflow)

    def test_source_sync_script_has_utf8_bom_for_windows_powershell(self):
        raw = (ROOT / "publish_source_branch.ps1").read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        script = raw.decode("utf-8-sig")
        self.assertIn("build/bci-update-system", script)
        self.assertIn("Введите YES", script)
        self.assertIn("FIRST_WINDOWS_BUILD_RU.md", script)
        self.assertIn("publish_source_branch.bat", script)
        self.assertIn("GitHub display name", script)
        self.assertIn("Enter a valid email address", script)

    def test_source_sync_script_uses_new_branch_and_requires_confirmation(self):
        script = (ROOT / "publish_source_branch.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("build/bci-update-system", script)
        self.assertIn("Введите YES", script)
        self.assertIn('git -C $ClonePath push origin "HEAD:$Branch"', script)
        self.assertNotIn("--force", script)
        self.assertNotIn("git reset --hard", script)

    def test_update_ui_and_installer_marker_are_wired(self):
        app = (ROOT / "bci_app.py").read_text(encoding="utf-8")
        updater = (ROOT / "bci_updater.py").read_text(encoding="utf-8")
        self.assertIn('notebook.add(update_tab, text="Обновления")', app)
        self.assertIn("_check_updates", app)
        self.assertIn("download_and_verify", app)
        self.assertIn("launch_verified_installer", app)
        self.assertIn("has_installer_marker", updater)
        self.assertIn("ShellExecuteW", updater)
        self.assertIn('"/VERYSILENT /NORESTART /CLOSEAPPLICATIONS"', updater)
        self.assertNotIn("/SUPPRESSMSGBOXES", updater)


if __name__ == "__main__":
    unittest.main()
