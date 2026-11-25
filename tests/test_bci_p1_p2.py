from __future__ import annotations

import json
import os
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

import pytest

from bci_core import (
    ModuleInfo,
    XmlRecord,
    _apply_profile_game_mode,
    _compare_mod_xml_records,
    _flatten_record,
    _make_fingerprint,
    _version_constraint_matches,
    analyze_load_order,
    export_report,
    format_current_order_status,
    format_load_order_export,
)
from bci_environment import (
    _discover_vortex_load_order,
    parse_load_order_json,
)
from bci_store import BCIStore


@pytest.mark.parametrize(
    ("actual", "expected", "status"),
    [
        ("v2.4.2.248", "v2.3.3", "vanilla_mismatch_only"),
        ("v1.5.7", "v1.4.0.*", "vanilla_mismatch_only"),
        ("v1.3.7", "v1.4.0.*", "incompatible"),
        ("v1.4.0", "v1.4.0", "satisfied"),
        ("v1.5.7", "[v1.0.0, v2.0.0)", "vanilla_mismatch_only"),
        ("v2.0.0", "[v1.0.0, v2.0.0)", "incompatible"),
        ("v2.0.0.248", "v1.0.0 - v2.0.0", "vanilla_mismatch_only"),
        ("v1.5.7", "v1.0.0-v2.0.0", "vanilla_mismatch_only"),
        ("e1.5.6", "e1.4.3.*", "vanilla_mismatch_only"),
        ("v1.5.6", "e1.4.3.*", "vanilla_mismatch_only"),
    ],
)
def test_butr_blse_version_constraints(actual: str, expected: str, status: str) -> None:
    assert _version_constraint_matches(actual, expected) == status


def test_version_constraint_unknown_for_malformed_or_missing_values() -> None:
    assert _version_constraint_matches("?", "v1.0.0") == "unknown"
    assert _version_constraint_matches("v1.0.0", "v1.*") == "unknown"
    assert _version_constraint_matches("v1.0.0", "[broken, v2.0.0)") == "unknown"


def test_vanilla_only_version_warning_has_zero_risk_and_true_incompatibility_is_medium() -> None:
    harmony = ModuleInfo(folder="Harmony", path="", module_id="Harmony", name="Harmony", version="v2.4.2.248")
    addon = ModuleInfo(
        folder="Addon", path="", module_id="Addon", name="Addon", version="v1.0.0",
        dependencies=[{"id": "Harmony", "optional": False, "version": "v2.3.3"}],
    )
    analysis = analyze_load_order([harmony, addon])
    info = next(issue for issue in analysis["issues"] if issue["kind"] == "dependency_version_vanilla_mismatch")
    assert info["severity"] == "Info"
    assert "требование butr выполнено" in info["description"].casefold()

    too_old = ModuleInfo(folder="Old", path="", module_id="Old", name="Old", version="v1.0.0")
    addon.dependencies = [{"id": "Old", "optional": False, "version": "v2.0.0"}]
    analysis = analyze_load_order([too_old, addon])
    mismatch = next(issue for issue in analysis["issues"] if issue["kind"] == "dependency_version_mismatch")
    assert mismatch["severity"] == "Medium"


def _vortex_state(profile_id: str = "profile-sp-01", *, mode: bool | None = False) -> dict:
    features = {} if mode is None else {"bannerlord_multiplayer": mode}
    return {
        "settings": {"profiles": {"activeProfileId": profile_id}},
        "persistent": {
            "profiles": {
                profile_id: {
                    "gameId": "mountandblade2bannerlord",
                    "name": "My Singleplayer Profile",
                    "features": features,
                }
            }
        },
    }


def test_vortex_profile_loads_14_enabled_sp_modules_without_multiplayer(tmp_path: Path) -> None:
    appdata = tmp_path / "AppData"
    snapshot = appdata / "Vortex" / "temp" / "state_backups_full" / "hourly.json"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text(json.dumps(_vortex_state()), encoding="utf-8")

    game_root = tmp_path / "Bannerlord"
    game_root.mkdir()
    entries = [
        {
            "id": f"Mod{i}", "name": f"Mod {i}", "isSelected": True,
            "isDisabled": False, "index": i, "isOfficial": i < 7,
        }
        for i in range(14)
    ]
    (game_root / "profile-sp-01_loadorder.json").write_text(json.dumps(entries), encoding="utf-8")

    with patch.dict("os.environ", {"APPDATA": str(appdata)}), patch(
        "bci_environment.time.time", return_value=snapshot.stat().st_mtime + 30
    ):
        order, source = _discover_vortex_load_order(game_root, {f"mod{i}" for i in range(14)} | {"multiplayer"}, [])

    assert source == str(game_root / "profile-sp-01_loadorder.json")
    assert len(order) == 14
    assert "multiplayer" not in order
    assert all(item["game_mode"] == "singleplayer" for item in order.values())
    assert all(item["enabled"] and item["enabled_known"] for item in order.values())


def test_vortex_json_selection_and_lock_state_are_not_confused(tmp_path: Path) -> None:
    path = tmp_path / "profile_loadorder.json"
    path.write_text(json.dumps([
        {"id": "EnabledLocked", "isSelected": True, "isDisabled": True, "index": 4},
        {"id": "Disabled", "isSelected": False, "isDisabled": False, "index": 5},
    ]), encoding="utf-8")
    parsed = parse_load_order_json(path, strict=True)
    assert parsed["enabledlocked"]["enabled"] is True
    assert parsed["enabledlocked"]["is_locked"] is True
    assert parsed["disabled"]["enabled"] is False
    assert parsed["disabled"]["is_locked"] is False


def test_vortex_profile_defaults_to_singleplayer_when_mode_feature_is_missing(tmp_path: Path) -> None:
    appdata = tmp_path / "AppData"
    snapshot = appdata / "Vortex" / "temp" / "state_backups_full" / "startup.json"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text(json.dumps(_vortex_state(mode=None)), encoding="utf-8")
    game_root = tmp_path / "Bannerlord"
    game_root.mkdir()
    profile_id = "profile-sp-01"
    sp_file = game_root / f"{profile_id}_loadorder.json"
    mp_file = game_root / f"{profile_id}_loadorder_multiplayer.json"
    sp_file.write_text(json.dumps([{"id": "Native", "isSelected": True, "isDisabled": False}]), encoding="utf-8")
    mp_file.write_text(json.dumps([{"id": "Multiplayer", "isSelected": True, "isDisabled": False}]), encoding="utf-8")
    notes: list[str] = []
    with patch.dict("os.environ", {"APPDATA": str(appdata)}), patch(
        "bci_environment.time.time", return_value=snapshot.stat().st_mtime + 10
    ):
        order, source = _discover_vortex_load_order(game_root, {"native", "multiplayer"}, notes)
    assert source == str(sp_file)
    assert list(order) == ["native"]
    assert all(entry["game_mode"] == "singleplayer" for entry in order.values())


def test_singleplayer_scope_excludes_network_modules_unless_explicitly_enabled() -> None:
    normal = ModuleInfo(folder="Native", path="", module_id="Native", name="Native", version="v1.0.0")
    unknown_mp = ModuleInfo(folder="Multiplayer", path="", module_id="Multiplayer", name="Multiplayer", version="v1.0.0", is_multiplayer_module=True)
    disabled_mp = ModuleInfo(folder="Dedicated", path="", module_id="DedicatedCustomServerTeam", name="Dedicated", version="v1.0.0", is_server_module=True, enabled=False, enabled_known=True)
    enabled_mp = ModuleInfo(folder="MPExplicit", path="", module_id="MPExplicit", name="MP explicit", version="v1.0.0", is_multiplayer_module=True, enabled=True, enabled_known=True)
    scoped = _apply_profile_game_mode([normal, unknown_mp, disabled_mp, enabled_mp], "singleplayer")
    assert [module.module_id for module in scoped] == ["Native", "MPExplicit"]
    assert unknown_mp.excluded_by_game_mode == "singleplayer"
    assert disabled_mp.excluded_by_game_mode == "singleplayer"


@pytest.mark.parametrize(
    ("analysis", "expected"),
    [
        ({"current_order_known": False, "violations": []}, "Не проверено (профиль порядка не подтверждён)"),
        ({"current_order_known": True, "violations": []}, "0"),
        ({"current_order_known": True, "violations": [{"modules": ["A", "B"]}]}, "1"),
    ],
)
def test_three_current_order_statuses(analysis: dict, expected: str) -> None:
    assert format_current_order_status(analysis) == expected


def test_current_order_unknown_is_rendered_consistently_in_txt_and_html(tmp_path: Path) -> None:
    report = {
        "app_version": "0.5.0-alpha",
        "generated_at": "2026-11-24T12:00:00Z",
        "game_root_path": str(tmp_path),
        "analysis_scope": "Профиль не подтверждён",
        "summary": {
            "modules_confirmed_enabled": 0, "modules_in_analysis": 0,
            "modules_enabled": 0, "modules_found": 0, "findings_total": 0, "xml_records": 0,
        },
        "ranking": [], "findings": [], "limitations": [],
        "load_order_analysis": {
            "current_order_known": False, "violations": [],
            "recommendation_status": "provisional", "constraints": [],
            "pairwise_order_evidence": {"determined_pairs": 0, "total_pairs": 0, "coverage_pct": None},
            "manual_use_warnings": [], "recommended_order": [], "file_interactions": [],
        },
        "performance": {"scan_total_ms": 0, "stages_ms": {}, "stats": {}},
    }
    expected = "Не проверено (профиль порядка не подтверждён)"
    assert expected in format_load_order_export(report)
    _json_path, html_path = export_report(report, tmp_path / "reports")
    assert expected in html_path.read_text(encoding="utf-8")


def test_flatten_record_prefers_semantic_slot_and_keeps_position_only_for_anonymous_nodes() -> None:
    element = ET.fromstring(
        '<NPCCharacter id="npc"><equipment slot="Item0"><item id="sword"/></equipment>'
        '<equipment slot="Item1"><item id="shield"/></equipment>'
        '<ordinary>first</ordinary><ordinary>second</ordinary></NPCCharacter>'
    )
    flattened = _flatten_record(element, ["id"])
    keys = list(flattened)
    assert any('equipment[@slot="Item0"]' in key for key in keys)
    assert any('equipment[@slot="Item1"]' in key for key in keys)
    assert not any(key.startswith("./equipment[1]/") or key.startswith("./equipment[2]/") for key in keys)
    assert any(key.startswith("./ordinary[1]/") for key in keys)
    assert any(key.startswith("./ordinary[2]/") for key in keys)


def test_flatten_record_prefers_stable_id_over_mutable_descriptive_attributes() -> None:
    first = ET.fromstring('<root><item id="stable-id" name="Old label" type="weapon"/></root>')
    second = ET.fromstring('<root><item id="stable-id" name="New label" type="weapon"/></root>')
    first_flat = _flatten_record(first, ["id"])
    second_flat = _flatten_record(second, ["id"])
    assert set(first_flat) == set(second_flat)
    assert first_flat['./item[@id="stable-id"]/@name'] == "Old label"
    assert second_flat['./item[@id="stable-id"]/@name'] == "New label"


def test_rbm_addon_relationship_and_patch_candidate_are_classified() -> None:
    rbm = ModuleInfo(folder="RBM", path="", module_id="RBM", name="RBM", version="v1.0.0")
    rbm_ws = ModuleInfo(
        folder="RBM_WS", path="", module_id="RBM_WS", name="RBM WS", version="v1.0.0",
        dependencies=[{"id": "RBM", "optional": False, "version": ""}],
    )
    patch_module = ModuleInfo(
        folder="CompatibilityPatch", path="", module_id="CompatibilityPatch", name="Compatibility Patch", version="v1.0.0",
        dependencies=[
            {"id": "RBM", "optional": False, "version": ""},
            {"id": "RBM_WS", "optional": False, "version": ""},
        ],
    )
    graph = analyze_load_order([rbm, rbm_ws, patch_module])
    object_key = ("npccharacters", "skill", '{"id":"Athletics"}')

    def record(module_id: str, value: str) -> XmlRecord:
        return XmlRecord(
            module_id=module_id, module_name=module_id, table="NPCCharacters", tag="skill",
            key='{"id":"Athletics"}', attributes={"@value": value}, source=f"{module_id}/ModuleData/skills.xml",
            key_confidence="XSD",
        )

    records = {
        "RBM": {object_key: record("RBM", "80")},
        "RBM_WS": {object_key: record("RBM_WS", "130")},
        "CompatibilityPatch": {object_key: record("CompatibilityPatch", "120")},
    }
    stats = {"mod_pairs_considered": 0, "mod_pairs_with_shared_xml": 0, "shared_xml_objects_compared": 0}
    pairs = _compare_mod_xml_records([rbm, rbm_ws, patch_module], records, {}, stats, graph["constraints"])
    relation = pairs[("RBM", "RBM_WS")]["relationship"]
    assert relation["classification"] == "addon_override_candidate"
    candidates = pairs[("RBM", "RBM_WS")]["compatibility_patch_candidates"]
    assert candidates[0]["module_id"] == "CompatibilityPatch"
    assert candidates[0]["classification"] == "candidate_not_confirmed"


def test_persistent_sha256_cache_hit_and_force_rehash(tmp_path: Path) -> None:
    module_dir = tmp_path / "Modules" / "TestMod"
    module_dir.mkdir(parents=True)
    source = module_dir / "SubModule.xml"
    source.write_text("<Module><Id value='TestMod'/></Module>", encoding="utf-8")
    module = ModuleInfo(folder="TestMod", path=str(module_dir), module_id="TestMod", name="Test Mod", version="v1.0.0")
    store = BCIStore(tmp_path / "bci.sqlite")

    first_stats: dict[str, int] = {}
    pending: dict[str, tuple[int, int, str]] = {}
    first_fingerprint = _make_fingerprint(module, [source], first_stats, {}, pending)
    assert first_stats["fingerprinted_files"] == 1
    assert store.upsert_file_hash_cache(pending) == 1

    second_stats: dict[str, int] = {}
    cache = store.get_file_hash_cache()
    second_pending: dict[str, tuple[int, int, str]] = {}
    second_fingerprint = _make_fingerprint(module, [source], second_stats, cache, second_pending)
    assert second_fingerprint == first_fingerprint
    assert second_stats["fingerprint_cache_hits"] == 1
    assert second_stats.get("bytes_hashed", 0) == 0
    assert second_pending == {}

    # A changed file signature must invalidate the cached digest automatically.
    source.write_text("<Module><Id value='TestMod'/><Name value='Updated'/></Module>", encoding="utf-8")
    changed_stats: dict[str, int] = {}
    changed_pending: dict[str, tuple[int, int, str]] = {}
    changed_fingerprint = _make_fingerprint(
        module, [source], changed_stats, store.get_file_hash_cache(), changed_pending
    )
    assert changed_fingerprint != first_fingerprint
    assert changed_stats["fingerprint_cache_misses"] == 1
    assert changed_stats["bytes_hashed"] == source.stat().st_size
    assert store.upsert_file_hash_cache(changed_pending) == 1

    force_stats: dict[str, int] = {}
    force_pending: dict[str, tuple[int, int, str]] = {}
    forced_fingerprint = _make_fingerprint(
        module, [source], force_stats, store.get_file_hash_cache(), force_pending, force_rehash=True
    )
    assert forced_fingerprint == changed_fingerprint
    assert force_stats["fingerprinted_files"] == 1
    assert force_stats["bytes_hashed"] == source.stat().st_size
    assert force_stats["fingerprinted_files"] == 1
    assert force_stats["bytes_hashed"] == source.stat().st_size
    assert store.clear_file_hash_cache() == 1
    assert store.get_file_hash_cache() == {}


def _write_test_module(modules_root: Path, folder: str, module_id: str, *, category: str = "Singleplayer", dependencies: str = "") -> None:
    module_dir = modules_root / folder
    data_dir = module_dir / "ModuleData"
    data_dir.mkdir(parents=True)
    manifest = (
        "<Module>"
        f"<Name value='{folder}'/><Id value='{module_id}'/><Version value='v1.0.0'/>"
        f"<ModuleCategory value='{category}'/><DependedModules>{dependencies}</DependedModules>"
        "</Module>"
    )
    (module_dir / "SubModule.xml").write_text(manifest, encoding="utf-8")
    (data_dir / "items.xml").write_text("<Items><Item id='test_item' value='1'/></Items>", encoding="utf-8")


def test_scan_modules_uses_vortex_sp_profile_and_excludes_unselected_multiplayer(tmp_path: Path) -> None:
    from bci_core import scan_modules

    game_root = tmp_path / "Bannerlord"
    modules_root = game_root / "Modules"
    modules_root.mkdir(parents=True)
    active_ids = ["Native", "Harmony", "ButterLib", "UIExtenderEx", "ModA", "ModB", "ModC", "ModD", "ModE", "ModF", "ModG", "ModH", "ModI", "ModJ"]
    for module_id in active_ids:
        _write_test_module(modules_root, module_id, module_id)
    _write_test_module(modules_root, "Multiplayer", "Multiplayer", category="Multiplayer")

    profile_id = "YcEPUSS3n"
    profile_entries = [
        {"id": module_id, "name": module_id, "isSelected": True, "isDisabled": False, "index": index, "isOfficial": module_id == "Native"}
        for index, module_id in enumerate(active_ids)
    ]
    appdata = tmp_path / "AppData"
    vortex_mods = appdata / "Vortex" / "mountandblade2bannerlord" / "mods"
    vortex_mods.mkdir(parents=True)
    order_path = vortex_mods / f"{profile_id}_loadorder.json"
    order_path.write_text(json.dumps(profile_entries), encoding="utf-8")

    # A stale/invalid BEM registry must not block an already resolved Vortex profile.
    local_app = tmp_path / "LocalAppData"
    bem_root = local_app / "Bannerlord Environment Manager"
    bem_root.mkdir(parents=True)
    (bem_root / "instances.json").write_text(json.dumps({"GamesRoot": str(tmp_path / "missing-bem-root")}), encoding="utf-8")

    store = BCIStore(tmp_path / "local" / "bci.sqlite")

    with patch.dict("os.environ", {"APPDATA": str(appdata), "LOCALAPPDATA": str(local_app)}):
        report = scan_modules(game_root, store=store)

    assert report["profile_game_mode"] == "singleplayer"
    assert report["profile_info"]["profile_id"] == profile_id
    assert report["profile_info"]["selection_method"] == "latest_recent_profile_file"
    assert report["profile_info"]["active_profile_verified"] is False
    assert report["profile_info"]["load_order_path"] == str(order_path)
    assert report["enabled_state_status"] == "known"
    assert report["summary"]["modules_found"] == 15
    assert report["summary"]["modules_in_analysis"] == 14
    assert report["summary"]["modules_confirmed_enabled"] == 14
    assert "multiplayer" not in {row["module_id"].casefold() for row in report["load_order_analysis"]["recommended_order"]}
    assert report["load_order_known"] is True
    assert report["load_order_analysis"]["current_order_known"] is True
    assert any(row.get("path") == str(order_path) and row.get("status") == "accepted_inferred" for row in report["launcher_discovery_details"])
    assert not any("Bannerlord Environment Manager" in row.get("path", "") for row in report["launcher_discovery_details"])


def test_vortex_provider_marker_alone_does_not_prove_complete_profile(tmp_path: Path) -> None:
    path = tmp_path / "partial_loadorder.json"
    path.write_text(json.dumps([
        {
            "id": "Native",
            "data": {"moduleInfoExtended": {"moduleProviderType": "vortex", "id": "Native"}},
        },
        {"id": "Harmony", "isSelected": True},
    ]), encoding="utf-8")

    parsed = parse_load_order_json(path, strict=True)

    assert parsed["native"]["source_type"] == "Vortex (BUTR)"
    assert parsed["native"]["profile_complete"] is False
    assert parsed["harmony"]["profile_complete"] is False


def test_vortex_export_document_is_complete_only_when_all_entries_have_enabled_state(tmp_path: Path) -> None:
    complete_path = tmp_path / "export.json"
    complete_path.write_text(json.dumps({
        "version": 1,
        "loadOrderId": "default",
        "entries": [
            {"id": "Native", "enabled": True, "name": "Native"},
            {"id": "Harmony", "enabled": False, "name": "Harmony"},
        ],
    }), encoding="utf-8")
    incomplete_path = tmp_path / "partial_export.json"
    incomplete_path.write_text(json.dumps({
        "version": 1,
        "loadOrderId": "default",
        "entries": [
            {"id": "Native", "enabled": True, "name": "Native"},
            {"id": "Harmony", "name": "Harmony"},
        ],
    }), encoding="utf-8")

    complete = parse_load_order_json(complete_path, strict=True)
    incomplete = parse_load_order_json(incomplete_path, strict=True)

    assert all(entry["profile_complete"] for entry in complete.values())
    assert not any(entry["profile_complete"] for entry in incomplete.values())


def test_fresh_state_prefers_canonical_vortex_appdata_path_over_newer_legacy_copy(tmp_path: Path) -> None:
    appdata = tmp_path / "AppData"
    snapshot = appdata / "Vortex" / "temp" / "state_backups_full" / "hourly.json"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text(json.dumps(_vortex_state("profile-sp-01")), encoding="utf-8")

    game_root = tmp_path / "Bannerlord"
    game_root.mkdir()
    entries = [{"id": "Native", "isSelected": True, "isDisabled": False, "index": 0}]
    appdata_order = appdata / "Vortex" / "mountandblade2bannerlord" / "mods" / "profile-sp-01_loadorder.json"
    appdata_order.parent.mkdir(parents=True)
    appdata_order.write_text(json.dumps(entries), encoding="utf-8")
    legacy_order = game_root / "profile-sp-01_loadorder.json"
    legacy_order.write_text(json.dumps(entries + [{"id": "LegacyOnly", "isSelected": True, "isDisabled": False, "index": 1}]), encoding="utf-8")
    os.utime(appdata_order, (snapshot.stat().st_mtime - 10, snapshot.stat().st_mtime - 10))
    os.utime(legacy_order, (snapshot.stat().st_mtime + 10, snapshot.stat().st_mtime + 10))

    with patch.dict("os.environ", {"APPDATA": str(appdata)}), patch(
        "bci_environment.time.time", return_value=snapshot.stat().st_mtime + 20
    ):
        order, source = _discover_vortex_load_order(game_root, {"native", "legacyonly"}, [])

    assert source == str(appdata_order)
    assert set(order) == {"native"}


def test_manual_vortex_export_document_excludes_installed_but_unlisted_multiplayer(tmp_path: Path) -> None:
    from bci_core import scan_modules

    game_root = tmp_path / "Bannerlord"
    modules_root = game_root / "Modules"
    modules_root.mkdir(parents=True)
    _write_test_module(modules_root, "Native", "Native")
    _write_test_module(modules_root, "Harmony", "Harmony")
    _write_test_module(modules_root, "Multiplayer", "Multiplayer", category="Multiplayer")

    def entry(module_id: str, index: int, *, singleplayer: bool) -> dict:
        return {
            "id": module_id,
            "enabled": True,
            "name": module_id,
            "index": index,
            "data": {
                "moduleInfoExtended": {
                    "id": module_id,
                    "name": module_id,
                    "moduleProviderType": "Vortex",
                    "isOfficial": module_id == "Native",
                    "isSingleplayerModule": singleplayer,
                    "isMultiplayerModule": False,
                    "isServerModule": False,
                }
            },
        }

    profile_path = tmp_path / "loadorder.json"
    profile_path.write_text(json.dumps({
        "version": 1,
        "loadOrderId": "default",
        "entries": [entry("Native", 0, singleplayer=False), entry("Harmony", 1, singleplayer=True)],
    }), encoding="utf-8")

    report = scan_modules(game_root, profile_path, store=BCIStore(tmp_path / "bci.sqlite"))

    assert report["profile_info"]["profile_complete"] is True
    assert report["profile_game_mode"] == "singleplayer"
    assert report["summary"]["modules_found"] == 3
    assert report["summary"]["modules_in_analysis"] == 2
    assert report["summary"]["modules_confirmed_enabled"] == 2
    assert "Multiplayer" not in {row["module_id"] for row in report["load_order_analysis"]["recommended_order"]}
    assert report["load_order_analysis"]["current_order_known"] is True
