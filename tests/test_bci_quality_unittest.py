from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from bci_core import ModuleInfo, _collect_module_files, analyze_load_order, export_report, scan_modules
from bci_environment import _discover_vortex_load_order


def _vortex_state() -> dict:
    return {
        "settings": {"profiles": {"activeProfileId": "unittest-profile"}},
        "persistent": {
            "profiles": {
                "unittest-profile": {
                    "gameId": "mountandblade2bannerlord",
                    "name": "Unittest Campaign",
                    "features": {},
                }
            }
        },
    }


def _write_module(root: Path, folder: str, module_id: str, version: str, *, dependencies: str = "", metadata: str = "") -> Path:
    module = root / folder
    module.mkdir(parents=True, exist_ok=True)
    (module / "SubModule.xml").write_text(
        "<Module>"
        f"<Name value='{folder}'/><Id value='{module_id}'/><Version value='{version}'/>"
        f"<DependedModules>{dependencies}</DependedModules>"
        f"<DependedModuleMetadatas>{metadata}</DependedModuleMetadatas>"
        "</Module>",
        encoding="utf-8",
    )
    (module / "ModuleData").mkdir(exist_ok=True)
    return module


class BCI051QualityTests(unittest.TestCase):
    def test_optional_naval_dlc_and_duplicate_version_rules_are_info_zero(self) -> None:
        naval = ModuleInfo(folder="NavalDLC", path="", module_id="NavalDLC", name="NavalDLC", version="v1.3.7")
        diplomacy = ModuleInfo(
            folder="Diplomacy", path="", module_id="Diplomacy", name="Diplomacy", version="v1.5.7",
            dependencies=[{"id": "NavalDLC", "version": "v1.4.0.*", "optional": True}],
            ordering_metadata=[{
                "id": "NavalDLC", "order": "LoadBeforeThis", "version": "v1.4.0.*", "optional": True,
            }],
        )
        result = analyze_load_order([naval, diplomacy])
        issues = [issue for issue in result["issues"] if "version" in issue["kind"]]
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["severity"], "Info")
        self.assertTrue(issues[0]["optional"])
        self.assertEqual(set(issues[0]["sources"]), {"DependedModules", "DependedModuleMetadata"})

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Bannerlord"
            modules = root / "Modules"
            modules.mkdir(parents=True)
            _write_module(modules, "Native", "Native", "v1.5.7")
            _write_module(modules, "NavalDLC", "NavalDLC", "v1.3.7")
            _write_module(
                modules, "Diplomacy", "Bannerlord.Diplomacy", "v1.5.7",
                dependencies='<DependedModule Id="NavalDLC" DependentVersion="v1.4.0.*" Optional="true"/>',
                metadata='<DependedModuleMetadata id="NavalDLC" order="LoadBeforeThis" version="v1.4.0.*" optional="true"/>',
            )
            profile = Path(tmp) / "unittest_loadorder.json"
            profile.write_text(json.dumps([
                {"id": "Native", "isSelected": True, "isDisabled": False, "index": 0},
                {"id": "NavalDLC", "isSelected": True, "isDisabled": False, "index": 1},
                {"id": "Bannerlord.Diplomacy", "isSelected": True, "isDisabled": False, "index": 2},
            ]), encoding="utf-8")
            report = scan_modules(root, profile)
            findings = [finding for finding in report["findings"] if finding["evidence"].get("target") == "NavalDLC" and "version" in finding["evidence"].get("kind", "")]
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0]["severity"], "Info")
            self.assertEqual(findings[0]["risk_points"], 0)

    def test_vortex_folder_markers_never_enter_resource_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            module = Path(tmp) / "Module"
            module.mkdir()
            (module / "SubModule.xml").write_text("<Module/>", encoding="utf-8")
            markers = [
                module / "GUI" / "__folder_managed_by_vortex",
                module / "GUI" / "Brushes" / "__folder_managed_by_vortex",
                module / "GUI" / "Prefabs" / "__folder_managed_by_vortex",
            ]
            for marker in markers:
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text("marker", encoding="utf-8")
            inventory = _collect_module_files(module)
            self.assertFalse(any(path.name.casefold() == "__folder_managed_by_vortex" for path in inventory.resource_files))
            self.assertTrue(all(path in inventory.all_files for path in markers))

    def test_vortex_snapshot_freshness_and_diagnostics_are_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            appdata = Path(tmp) / "AppData"
            backups = appdata / "Vortex" / "temp" / "state_backups_full"
            backups.mkdir(parents=True)
            snapshot = backups / "hourly.json"
            snapshot.write_text(json.dumps(_vortex_state()), encoding="utf-8")
            game_root = Path(tmp) / "Bannerlord"
            game_root.mkdir()
            vortex_mods = appdata / "Vortex" / "mountandblade2bannerlord" / "mods"
            vortex_mods.mkdir(parents=True)
            order_path = vortex_mods / "unittest-profile_loadorder.json"
            order_path.write_text(json.dumps([
                {"id": "Native", "isSelected": True, "isDisabled": False, "index": 0}
            ]), encoding="utf-8")
            diagnostics: list[dict] = []
            profile_info: dict = {}
            with patch.dict(os.environ, {"APPDATA": str(appdata)}), patch(
                "bci_environment.time.time", return_value=snapshot.stat().st_mtime + 5
            ):
                order, source = _discover_vortex_load_order(
                    game_root, {"native"}, [], diagnostics=diagnostics, profile_info=profile_info
                )
            self.assertEqual(source, str(order_path))
            self.assertEqual(profile_info["profile_id"], "unittest-profile")
            self.assertEqual(profile_info["profile_name"], "Unittest Campaign")
            self.assertEqual(profile_info["game_mode"], "singleplayer")
            self.assertEqual(
                profile_info["snapshot_created_at"],
                datetime.fromtimestamp(snapshot.stat().st_mtime, timezone.utc).isoformat(),
            )
            self.assertTrue(any(row.get("path") == str(snapshot) and row.get("status") == "accepted" for row in diagnostics))

            snapshot.unlink()
            stale = backups / "manual.json"
            stale.write_text(json.dumps(_vortex_state()), encoding="utf-8")
            old = stale.stat().st_mtime - 3600
            os.utime(stale, (old, old))
            stale_diagnostics: list[dict] = []
            notes: list[str] = []
            profile_info: dict = {}
            with patch.dict(os.environ, {"APPDATA": str(appdata)}):
                order, source = _discover_vortex_load_order(
                    game_root, {"native"}, notes, diagnostics=stale_diagnostics, profile_info=profile_info
                )
            self.assertEqual(source, str(order_path))
            self.assertTrue(order["native"]["enabled"])
            self.assertEqual(profile_info["selection_method"], "latest_recent_profile_file")
            self.assertFalse(profile_info["active_profile_verified"])
            self.assertTrue(any(row.get("path") == str(stale) and row.get("status") == "stale" for row in stale_diagnostics))
            self.assertTrue(any("устарел" in note.casefold() for note in notes))
            self.assertTrue(any(row.get("path", "").endswith("startup.json") and row.get("status") == "not_found" for row in stale_diagnostics))

    def test_profile_metadata_and_discovery_diagnostics_appear_in_html(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Bannerlord"
            modules = root / "Modules"
            modules.mkdir(parents=True)
            _write_module(modules, "Native", "Native", "v1.5.7")
            profile = root / "campaign-456_loadorder.json"
            profile.write_text(json.dumps([
                {"id": "Native", "isSelected": True, "isDisabled": False, "index": 0}
            ]), encoding="utf-8")
            report = scan_modules(root, profile)
            _json_path, html_path = export_report(report, Path(tmp) / "reports")
            html_text = html_path.read_text(encoding="utf-8")
            self.assertEqual(report["profile_info"]["profile_id"], "campaign-456")
            self.assertEqual(report["profile_info"]["game_mode"], "singleplayer")
            self.assertTrue(report["profile_info"]["load_order_created_at"])
            self.assertIn("Профиль и диагностика обнаружения", html_text)
            self.assertIn("campaign-456", html_text)
            self.assertIn("Singleplayer", html_text)
            self.assertIn(str(profile), html_text)
            self.assertIn("accepted_manual", html_text)


if __name__ == "__main__":
    unittest.main()
