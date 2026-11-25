from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from bci_core import ModuleInfo, _collect_module_files, analyze_load_order, export_report, scan_modules
from bci_environment import _discover_vortex_load_order


def _write_module(
    modules_root: Path,
    folder: str,
    module_id: str,
    name: str,
    version: str,
    *,
    dependencies: str = "",
    metadata: str = "",
    official: bool = False,
) -> Path:
    module_dir = modules_root / folder
    module_dir.mkdir(parents=True, exist_ok=True)
    module_xml = (
        "<Module>"
        f"<Name value='{name}'/><Id value='{module_id}'/><Version value='{version}'/>"
        f"<Official value='{str(official).lower()}'/>"
        f"<DependedModules>{dependencies}</DependedModules>"
        f"<DependedModuleMetadatas>{metadata}</DependedModuleMetadatas>"
        "</Module>"
    )
    (module_dir / "SubModule.xml").write_text(module_xml, encoding="utf-8")
    (module_dir / "ModuleData").mkdir(exist_ok=True)
    return module_dir


def _vortex_state(profile_id: str = "profile-sp-01") -> dict:
    return {
        "settings": {"profiles": {"activeProfileId": profile_id}},
        "persistent": {
            "profiles": {
                profile_id: {
                    "gameId": "mountandblade2bannerlord",
                    "name": "BCI Test Singleplayer",
                    "features": {},
                }
            }
        },
    }


def test_optional_diplomacy_naval_version_is_info_zero_and_deduplicated(tmp_path: Path) -> None:
    game_root = tmp_path / "Bannerlord"
    modules_root = game_root / "Modules"
    modules_root.mkdir(parents=True)
    _write_module(modules_root, "Native", "Native", "Native", "v1.5.7", official=True)
    _write_module(modules_root, "NavalDLC", "NavalDLC", "Naval DLC", "v1.3.7", official=True)
    _write_module(
        modules_root,
        "Diplomacy",
        "Bannerlord.Diplomacy",
        "Diplomacy",
        "v1.5.7",
        dependencies=(
            '<DependedModule Id="NavalDLC" DependentVersion="v1.4.0.*" Optional="true"/>'
        ),
        metadata=(
            '<DependedModuleMetadata id="NavalDLC" order="LoadBeforeThis" '
            'version="v1.4.0.*" optional="true"/>'
        ),
    )
    profile = tmp_path / "profile-test_loadorder.json"
    profile.write_text(json.dumps([
        {"id": "Native", "isSelected": True, "isDisabled": False, "index": 0},
        {"id": "NavalDLC", "isSelected": True, "isDisabled": False, "index": 1},
        {"id": "Bannerlord.Diplomacy", "isSelected": True, "isDisabled": False, "index": 2},
    ]), encoding="utf-8")

    report = scan_modules(game_root, profile)
    naval_findings = [
        finding for finding in report["findings"]
        if finding.get("evidence", {}).get("target") == "NavalDLC"
        and "version" in finding.get("evidence", {}).get("kind", "")
    ]

    assert len(naval_findings) == 1
    finding = naval_findings[0]
    assert finding["severity"] == "Info"
    assert finding["risk_points"] == 0
    assert finding["evidence"]["optional"] is True
    assert finding["evidence"]["sources"] == ["DependedModules", "DependedModuleMetadata"]
    assert "необязательное" in finding["description"].casefold()


def test_version_rules_merge_only_when_semantic_constraint_matches() -> None:
    harmony = ModuleInfo(
        folder="Harmony", path="", module_id="Harmony", name="Harmony", version="v1.3.0"
    )
    mcm = ModuleInfo(
        folder="MCM", path="", module_id="MCM", name="MCM", version="v5.0.0",
        dependencies=[{"id": "Harmony", "optional": False, "version": "v1.4.0"}],
        ordering_metadata=[{
            "id": "Harmony", "order": "LoadBeforeThis", "version": "v1.4.0.*", "optional": False,
        }],
    )
    merged = analyze_load_order([harmony, mcm])["issues"]
    version_issues = [issue for issue in merged if "version" in issue.get("kind", "")]
    assert len(version_issues) == 1
    assert set(version_issues[0]["sources"]) == {"DependedModules", "DependedModuleMetadata"}
    assert len(version_issues[0]["expected_versions"]) == 2

    mcm.ordering_metadata = [{
        "id": "Harmony", "order": "LoadBeforeThis", "version": "v1.5.0", "optional": False,
    }]
    distinct = analyze_load_order([harmony, mcm])["issues"]
    assert len([issue for issue in distinct if "version" in issue.get("kind", "")]) == 2


def test_vortex_folder_markers_are_excluded_from_resource_inventory(tmp_path: Path) -> None:
    module_dir = tmp_path / "Module"
    module_dir.mkdir()
    (module_dir / "SubModule.xml").write_text("<Module/>", encoding="utf-8")
    marker_paths = [
        module_dir / "GUI" / "__folder_managed_by_vortex",
        module_dir / "GUI" / "Brushes" / "__folder_managed_by_vortex",
        module_dir / "GUI" / "Prefabs" / "__folder_managed_by_vortex",
        module_dir / "Assets" / "Nested" / "__folder_managed_by_vortex",
    ]
    for marker in marker_paths:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("vortex marker", encoding="utf-8")
    inventory = _collect_module_files(module_dir)
    assert all(path.name.casefold() != "__folder_managed_by_vortex" for path in inventory.resource_files)
    # Markers remain part of the filesystem inventory/fingerprint; only resource analysis ignores them.
    assert all(path in inventory.all_files for path in marker_paths)


def test_vortex_snapshot_diagnostics_include_profile_identity_and_utc_timestamp(tmp_path: Path) -> None:
    appdata = tmp_path / "AppData"
    backup_dir = appdata / "Vortex" / "temp" / "state_backups_full"
    backup_dir.mkdir(parents=True)
    snapshot = backup_dir / "hourly.json"
    snapshot.write_text(json.dumps(_vortex_state()), encoding="utf-8")
    game_root = tmp_path / "Bannerlord"
    game_root.mkdir()
    vortex_mods = appdata / "Vortex" / "mountandblade2bannerlord" / "mods"
    vortex_mods.mkdir(parents=True)
    order_path = vortex_mods / "profile-sp-01_loadorder.json"
    order_path.write_text(json.dumps([
        {"id": "Native", "isSelected": True, "isDisabled": False, "index": 0}
    ]), encoding="utf-8")
    other_profile = vortex_mods / "default-profile_loadorder.json"
    other_profile.write_text(json.dumps([
        {"id": "Other", "isSelected": True, "isDisabled": False, "index": 0}
    ]), encoding="utf-8")
    # The Vortex state snapshot selects profile-sp-01 even though the other profile file is newer.
    newer = other_profile.stat().st_mtime + 5
    os.utime(other_profile, (newer, newer))
    fixed_now = snapshot.stat().st_mtime + 45
    profile_info: dict = {}
    diagnostics: list[dict] = []

    with patch.dict(os.environ, {"APPDATA": str(appdata)}), patch("bci_environment.time.time", return_value=fixed_now):
        order, source = _discover_vortex_load_order(
            game_root, {"native", "other"}, [], diagnostics=diagnostics, profile_info=profile_info
        )

    assert source == str(order_path)
    assert set(order) == {"native"}
    assert order["native"]["enabled"] is True
    assert profile_info["profile_id"] == "profile-sp-01"
    assert profile_info["profile_name"] == "BCI Test Singleplayer"
    assert profile_info["game_mode"] == "singleplayer"
    assert profile_info["snapshot_created_at"] == datetime.fromtimestamp(snapshot.stat().st_mtime, timezone.utc).isoformat()
    assert any(item.get("path") == str(snapshot) and item.get("status") == "accepted" for item in diagnostics)
    assert any(item.get("path") == str(order_path) and item.get("status") == "accepted" for item in diagnostics)


def test_stale_vortex_snapshot_is_rejected_and_all_backup_paths_are_reported(tmp_path: Path) -> None:
    appdata = tmp_path / "AppData"
    backup_dir = appdata / "Vortex" / "temp" / "state_backups_full"
    backup_dir.mkdir(parents=True)
    stale = backup_dir / "manual.json"
    stale.write_text(json.dumps(_vortex_state()), encoding="utf-8")
    old_time = stale.stat().st_mtime - 60 * 60
    os.utime(stale, (old_time, old_time))
    game_root = tmp_path / "Bannerlord"
    game_root.mkdir()
    diagnostics: list[dict] = []
    notes: list[str] = []

    with patch.dict(os.environ, {"APPDATA": str(appdata)}):
        order, source = _discover_vortex_load_order(game_root, {"native"}, notes, diagnostics=diagnostics)

    assert order == {}
    assert source is None
    stale_entry = next(item for item in diagnostics if item.get("path") == str(stale))
    assert stale_entry["status"] == "stale"
    assert "30 мин" in stale_entry["reason"]
    assert any(str(backup_dir / "startup.json") == item.get("path") and item.get("status") == "not_found" for item in diagnostics)
    assert any(str(backup_dir / "hourly.json") == item.get("path") and item.get("status") == "not_found" for item in diagnostics)
    assert any("устарел" in note.casefold() for note in notes)


def test_manual_profile_metadata_and_html_diagnostics_are_exported(tmp_path: Path) -> None:
    game_root = tmp_path / "Bannerlord"
    modules_root = game_root / "Modules"
    modules_root.mkdir(parents=True)
    _write_module(modules_root, "Native", "Native", "Native", "v1.5.7", official=True)
    manual_profile = game_root / "profile-123_loadorder.json"
    manual_profile.write_text(json.dumps([
        {"id": "Native", "isSelected": True, "isDisabled": False, "index": 0}
    ]), encoding="utf-8")

    report = scan_modules(game_root, manual_profile)
    _, html_path = export_report(report, tmp_path / "reports")
    html_text = html_path.read_text(encoding="utf-8")

    assert report["profile_info"]["profile_id"] == "profile-123"
    assert report["profile_info"]["profile_name"] == "Не содержится в выбранном JSON"
    assert report["profile_info"]["game_mode"] == "singleplayer"
    assert report["profile_info"]["load_order_created_at"]
    assert any(item.get("status") == "accepted_manual" for item in report["launcher_discovery_details"])
    assert "Профиль и диагностика обнаружения" in html_text
    assert "profile-123" in html_text
    assert "Не содержится в выбранном JSON" in html_text
    assert "Singleplayer" in html_text
    assert str(manual_profile) in html_text
    assert "accepted_manual" in html_text


def test_stale_vortex_snapshot_reason_is_present_in_html_report(tmp_path: Path) -> None:
    game_root = tmp_path / "Bannerlord"
    modules_root = game_root / "Modules"
    modules_root.mkdir(parents=True)
    _write_module(modules_root, "Native", "Native", "Native", "v1.5.7", official=True)

    appdata = tmp_path / "AppData"
    backup_dir = appdata / "Vortex" / "temp" / "state_backups_full"
    backup_dir.mkdir(parents=True)
    stale = backup_dir / "manual.json"
    stale.write_text(json.dumps(_vortex_state()), encoding="utf-8")
    old_time = stale.stat().st_mtime - 2 * 60 * 60
    os.utime(stale, (old_time, old_time))

    with patch.dict(os.environ, {"APPDATA": str(appdata), "LOCALAPPDATA": str(tmp_path / "LocalAppData")}), patch(
        "bci_environment._documents_candidates", return_value=[]
    ):
        report = scan_modules(game_root)

    _, html_path = export_report(report, tmp_path / "reports")
    html_text = html_path.read_text(encoding="utf-8")

    assert any(item.get("path") == str(stale) and item.get("status") == "stale" for item in report["launcher_discovery_details"])
    assert str(stale) in html_text
    assert "Снимок устарел" in html_text
    assert "допустимо не более 30 мин" in html_text


def test_required_duplicate_rule_is_not_downgraded_by_optional_duplicate() -> None:
    harmony = ModuleInfo(
        folder="Harmony", path="", module_id="Harmony", name="Harmony", version="v1.3.0"
    )
    mod = ModuleInfo(
        folder="Mod", path="", module_id="Mod", name="Mod", version="v1.0.0",
        dependencies=[{"id": "Harmony", "optional": False, "version": "v1.4.0"}],
        ordering_metadata=[{
            "id": "Harmony", "order": "LoadBeforeThis", "version": "v1.4.0.*", "optional": True,
        }],
    )
    issues = [issue for issue in analyze_load_order([harmony, mod])["issues"] if "version" in issue.get("kind", "")]
    assert len(issues) == 1
    assert issues[0]["optional"] is False
    assert issues[0]["severity"] == "Medium"


def test_vortex_profile_files_with_ambiguous_recent_timestamps_are_not_guessed(tmp_path: Path) -> None:
    appdata = tmp_path / "AppData"
    vortex_mods = appdata / "Vortex" / "mountandblade2bannerlord" / "mods"
    vortex_mods.mkdir(parents=True)
    first = vortex_mods / "profile-a_loadorder.json"
    second = vortex_mods / "profile-b_loadorder.json"
    entries = [{"id": "Native", "isSelected": True, "isDisabled": False, "index": 0}]
    first.write_text(json.dumps(entries), encoding="utf-8")
    second.write_text(json.dumps(entries), encoding="utf-8")
    same_time = time.time()
    os.utime(first, (same_time, same_time))
    os.utime(second, (same_time + 0.5, same_time + 0.5))
    game_root = tmp_path / "Bannerlord"
    game_root.mkdir()
    diagnostics: list[dict] = []
    notes: list[str] = []

    with patch.dict(os.environ, {"APPDATA": str(appdata)}), patch(
        "bci_environment.time.time", return_value=same_time + 10
    ):
        order, source = _discover_vortex_load_order(
            game_root, {"native"}, notes, diagnostics=diagnostics
        )

    assert order == {}
    assert source is None
    assert any(item.get("status") == "ambiguous" for item in diagnostics)
    assert any("нельзя надёжно определить активный профиль" in note for note in notes)
