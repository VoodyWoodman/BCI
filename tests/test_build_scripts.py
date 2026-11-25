from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class WindowsBuildScriptTests(unittest.TestCase):
    def test_double_click_scripts_pause_at_shared_finish_label(self):
        for filename in ("build_windows_exe.bat", "build_windows_installer.bat"):
            content = (ROOT / filename).read_text(encoding="utf-8").replace("\r\n", "\n")
            self.assertIn(":finish", content)
            self.assertIn("pause >nul", content)
            self.assertIn('if "%PAUSE_AT_END%"=="1"', content)
            self.assertIn("endlocal & exit /b %RESULT%", content)

    def test_build_scripts_support_python_312_and_313_x64(self):
        for filename in ("build_windows_exe.bat", "build_windows_installer.bat"):
            content = (ROOT / filename).read_text(encoding="utf-8")
            self.assertIn("py -3.12", content)
            self.assertIn("py -3.13", content)
            self.assertIn("sys.maxsize > 2**32", content)


    def test_runtime_detection_requires_interpreter_sentinel(self):
        for filename in ("build_windows_exe.bat", "build_windows_installer.bat"):
            content = (ROOT / filename).read_text(encoding="utf-8").replace("\r\n", "\n")
            self.assertIn(":probe_runtime", content)
            self.assertIn("BCI_RUNTIME_OK", content)
            self.assertIn('findstr /X /C:"BCI_RUNTIME_OK"', content)
            self.assertIn('call :probe_runtime "py -3.12" 3.12', content)
            self.assertIn('call :probe_runtime "py -3.13" 3.13', content)
            self.assertIn('call :probe_runtime "python" 3.12', content)
            self.assertIn('call :probe_runtime "python" 3.13', content)
            self.assertIn('set "PROBE_RESULT=1"', content)
            self.assertIn('if not errorlevel 1 set "PROBE_RESULT=0"', content)
            self.assertNotIn('set "PROBE_RESULT=%ERRORLEVEL%"', content)
            labels = [line for line in content.splitlines() if line.startswith(":")]
            for label in (":detect_python", ":probe_runtime", ":error", ":finish"):
                self.assertEqual(labels.count(label), 1, f"{filename}: duplicate/missing {label}")

    def test_installer_calls_portable_builder_without_nested_pause(self):
        content = (ROOT / "build_windows_installer.bat").read_text(encoding="utf-8")
        self.assertIn("call build_windows_exe.bat --prepared --no-pause", content)

    def test_build_venvs_are_version_specific(self):
        for filename in ("build_windows_exe.bat", "build_windows_installer.bat"):
            content = (ROOT / filename).read_text(encoding="utf-8")
            self.assertIn(".build-venv-%PYTHON_VERSION%", content)

    def test_local_runtime_error_points_to_remote_github_build(self):
        for filename in ("build_windows_exe.bat", "build_windows_installer.bat"):
            content = (ROOT / filename).read_text(encoding="utf-8")
            self.assertIn("publish_source_branch.bat", content)
            self.assertIn("GitHub Actions", content)
            self.assertIn("This is NOT required to install or use BCI", content)


if __name__ == "__main__":
    unittest.main()
