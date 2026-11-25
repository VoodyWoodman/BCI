import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bci_core

from bci_core import (
    ModuleInfo,
    XmlRecord,
    _collect_module_files,
    _compare_mod_xml_records,
    analyze_load_order,
    baseline_directory,
    compare_with_baseline,
    export_report,
    export_load_order,
    format_load_order_export,
    make_baseline,
    scan_modules,
)
from bci_environment import discover_load_order, parse_launcher_data, parse_load_order_json, resolve_game_paths


def write_module(root: Path, folder: str, module_id: str, name: str, xml: str, deps: str = ""):
    module_dir = root / folder
    (module_dir / "ModuleData").mkdir(parents=True)
    (module_dir / "SubModule.xml").write_text(
        f"""<Module><Name value='{name}'/><Id value='{module_id}'/><Version value='v1.0.0'/><DependedModules>{deps}</DependedModules><Xmls><XmlNode><XmlName id='Items' path='items'/></XmlNode></Xmls></Module>""",
        encoding="utf-8",
    )
    (module_dir / "ModuleData" / "items.xml").write_text(xml, encoding="utf-8")
    return module_dir


class EnvironmentDetectionTests(unittest.TestCase):
    def test_launcher_xml_missing_enabled_is_unknown_but_explicit_false_is_known(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "LauncherData.xml"
            path.write_text(
                "<UserData><SingleplayerData><ModDatas>"
                "<UserModData><Id>UnknownMod</Id></UserModData>"
                "<UserModData><Id>DisabledMod</Id><IsSelected>false</IsSelected></UserModData>"
                "</ModDatas></SingleplayerData></UserData>",
                encoding="utf-8",
            )

            parsed = parse_launcher_data(path)

            self.assertTrue(parsed["unknownmod"]["enabled"])
            self.assertFalse(parsed["unknownmod"]["enabled_known"])
            self.assertFalse(parsed["disabledmod"]["enabled"])
            self.assertTrue(parsed["disabledmod"]["enabled_known"])

    def test_json_profile_does_not_invent_missing_enabled_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "profile.json"
            path.write_text(json.dumps({"entries": [
                {"id": "UnknownMod"},
                {"id": "DisabledMod", "enabled": "false"},
            ]}), encoding="utf-8")

            parsed = parse_load_order_json(path, strict=True)

            self.assertTrue(parsed["unknownmod"]["enabled"])
            self.assertFalse(parsed["unknownmod"]["enabled_known"])
            self.assertFalse(parsed["disabledmod"]["enabled"])
            self.assertTrue(parsed["disabledmod"]["enabled_known"])

    def test_bem_instance_maps_game_to_its_own_launcher_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            local_app = base / "LocalAppData"
            bem_root = local_app / "Bannerlord Environment Manager"
            games_root = base / "Games"
            game_root = base / "Games" / "Bannerlord 1" / "Game"
            instance_folder = games_root / "Bannerlord 1"
            game_root.mkdir(parents=True)
            instance_folder.mkdir(parents=True, exist_ok=True)
            (bem_root).mkdir(parents=True)
            (bem_root / "instances.json").write_text(json.dumps({
                "GamesRoot": str(games_root),
                "RestingInstanceId": "resting-instance",
                "ActiveInstanceId": "bannerlord-1",
            }), encoding="utf-8")
            (instance_folder / "instance.json").write_text(json.dumps({
                "Id": "bannerlord-1", "GameFolder": str(game_root),
            }), encoding="utf-8")

            expected = (
                instance_folder / "UserData" / "Documents" / "Mount and Blade II Bannerlord"
                / "Configs" / "LauncherData.xml"
            )
            expected.parent.mkdir(parents=True)
            expected.write_text(
                "<UserData><SingleplayerData><ModDatas>"
                "<UserModData><Id>ModA</Id><IsSelected>true</IsSelected></UserModData>"
                "</ModDatas></SingleplayerData></UserData>",
                encoding="utf-8",
            )

            # A newer named JSON snapshot matches the game module ID but is not the active load order.
            saved_profiles = bem_root / "profiles"
            saved_profiles.mkdir()
            (saved_profiles / "older-order.json").write_text(json.dumps({
                "entries": [{"id": "ModA", "enabled": False, "index": 0}]
            }), encoding="utf-8")

            with patch.dict("os.environ", {"LOCALAPPDATA": str(local_app)}), patch(
                "bci_environment._documents_candidates", return_value=[]
            ):
                order, source, notes = discover_load_order(game_root, {"moda"})

            self.assertEqual(Path(source), expected)
            self.assertTrue(order["moda"]["enabled"])
            self.assertTrue(order["moda"]["enabled_known"])
            self.assertTrue(any("экземпляр BEM" in note for note in notes))

    def test_bem_resting_instance_uses_windows_documents_launcher_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            local_app = base / "LocalAppData"
            bem_root = local_app / "Bannerlord Environment Manager"
            games_root = base / "Games"
            instance_folder = games_root / "RestingInstance"
            game_root = instance_folder / "Game"
            game_root.mkdir(parents=True)
            bem_root.mkdir(parents=True)
            (bem_root / "instances.json").write_text(json.dumps({
                "GamesRoot": str(games_root), "RestingInstanceId": "resting-id",
                "ActiveInstanceId": "resting-id",
            }), encoding="utf-8")
            (instance_folder / "instance.json").write_text(json.dumps({
                "Id": "resting-id", "GameFolder": str(game_root),
            }), encoding="utf-8")

            documents = base / "Documents"
            launcher = documents / "Mount and Blade II Bannerlord" / "Configs" / "LauncherData.xml"
            launcher.parent.mkdir(parents=True)
            launcher.write_text(
                "<UserData><SingleplayerData><ModDatas>"
                "<UserModData><Id>ModA</Id><IsSelected>true</IsSelected></UserModData>"
                "</ModDatas></SingleplayerData></UserData>", encoding="utf-8"
            )

            with patch.dict("os.environ", {"LOCALAPPDATA": str(local_app)}), patch(
                "bci_environment._documents_candidates", return_value=[documents]
            ):
                order, source, notes = discover_load_order(game_root, {"moda"})

            self.assertEqual(Path(source), launcher)
            self.assertTrue(order["moda"]["enabled"])
            self.assertTrue(any("стандартных Documents" in note for note in notes))

    def test_scan_modules_uses_the_selected_bem_instance_profile_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            local_app = base / "LocalAppData"
            bem_root = local_app / "Bannerlord Environment Manager"
            games_root = base / "Games"
            instance_folder = games_root / "Bannerlord 1"
            game_root = instance_folder / "Game"
            modules = game_root / "Modules"
            modules.mkdir(parents=True)
            write_module(modules, "Native", "Native", "Native", "<Items/>")
            write_module(modules, "Addon", "Addon", "Addon", "<Items/>")
            bem_root.mkdir(parents=True)
            (bem_root / "instances.json").write_text(json.dumps({
                "GamesRoot": str(games_root), "RestingInstanceId": "resting-instance",
                "ActiveInstanceId": "bannerlord-1",
            }), encoding="utf-8")
            (instance_folder / "instance.json").write_text(json.dumps({
                "Id": "bannerlord-1", "GameFolder": str(game_root),
            }), encoding="utf-8")
            launcher = (
                instance_folder / "UserData" / "Documents" / "Mount and Blade II Bannerlord"
                / "Configs" / "LauncherData.xml"
            )
            launcher.parent.mkdir(parents=True)
            launcher.write_text(
                "<UserData><SingleplayerData><ModDatas>"
                "<UserModData><Id>Native</Id><IsSelected>true</IsSelected></UserModData>"
                "<UserModData><Id>Addon</Id><IsSelected>false</IsSelected></UserModData>"
                "</ModDatas></SingleplayerData></UserData>", encoding="utf-8"
            )

            with patch.dict("os.environ", {"LOCALAPPDATA": str(local_app)}), patch(
                "bci_environment._documents_candidates", return_value=[]
            ):
                report = scan_modules(game_root)

            by_id = {item["module_id"].casefold(): item for item in report["modules"]}
            self.assertEqual(report["launcher_order_source_type"], "BEM")
            self.assertTrue(report["enabled_state_known"])
            self.assertTrue(report["load_order_known"])
            self.assertTrue(by_id["native"]["enabled"])
            self.assertFalse(by_id["addon"]["enabled"])
            self.assertTrue(by_id["native"]["enabled_known"])
            self.assertTrue(by_id["addon"]["enabled_known"])

    def test_bem_managed_game_does_not_fall_back_to_another_instances_documents(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            local_app = base / "LocalAppData"
            bem_root = local_app / "Bannerlord Environment Manager"
            games_root = base / "Games"
            instance_folder = games_root / "Bannerlord 1"
            game_root = instance_folder / "Game"
            game_root.mkdir(parents=True)
            bem_root.mkdir(parents=True)
            (bem_root / "instances.json").write_text(json.dumps({
                "GamesRoot": str(games_root), "RestingInstanceId": "resting",
                "ActiveInstanceId": "selected",
            }), encoding="utf-8")
            (instance_folder / "instance.json").write_text(json.dumps({
                "Id": "selected", "GameFolder": str(game_root),
            }), encoding="utf-8")
            other_docs = base / "Documents"
            other_launcher = other_docs / "Mount and Blade II Bannerlord" / "Configs" / "LauncherData.xml"
            other_launcher.parent.mkdir(parents=True)
            other_launcher.write_text(
                "<UserData><SingleplayerData><ModDatas>"
                "<UserModData><Id>ModA</Id><IsSelected>true</IsSelected></UserModData>"
                "</ModDatas></SingleplayerData></UserData>", encoding="utf-8"
            )

            with patch.dict("os.environ", {"LOCALAPPDATA": str(local_app)}), patch(
                "bci_environment._documents_candidates", return_value=[other_docs]
            ):
                order, source, notes = discover_load_order(game_root, {"moda"})

            self.assertEqual(order, {})
            self.assertIsNone(source)
            self.assertTrue(any("не найден" in note for note in notes))

    def test_bem_registry_without_matching_game_does_not_use_shared_documents(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            local_app = base / "LocalAppData"
            bem_root = local_app / "Bannerlord Environment Manager"
            games_root = base / "ManagedGames"
            other_folder = games_root / "ManagedInstance"
            other_folder.mkdir(parents=True)
            bem_root.mkdir(parents=True)
            (bem_root / "instances.json").write_text(json.dumps({
                "GamesRoot": str(games_root), "RestingInstanceId": "resting",
                "ActiveInstanceId": "managed",
            }), encoding="utf-8")
            (other_folder / "instance.json").write_text(json.dumps({
                "Id": "managed", "GameFolder": str(other_folder / "Game"),
            }), encoding="utf-8")

            # This file belongs to the BEM resting instance, not to the unregistered game selected below.
            docs = base / "Documents"
            launcher = docs / "Mount and Blade II Bannerlord" / "Configs" / "LauncherData.xml"
            launcher.parent.mkdir(parents=True)
            launcher.write_text(
                "<UserData><SingleplayerData><ModDatas>"
                "<UserModData><Id>ModA</Id><IsSelected>true</IsSelected></UserModData>"
                "</ModDatas></SingleplayerData></UserData>", encoding="utf-8"
            )
            unregistered_game = base / "OtherBannerlord"
            unregistered_game.mkdir()

            with patch.dict("os.environ", {"LOCALAPPDATA": str(local_app)}), patch(
                "bci_environment._documents_candidates", return_value=[docs]
            ):
                order, source, notes = discover_load_order(unregistered_game, {"moda"})

            self.assertEqual(order, {})
            self.assertIsNone(source)
            self.assertTrue(any("не сопоставлена" in note for note in notes))

    def test_recent_vortex_profile_file_is_inferred_without_active_state_and_does_not_fall_back_to_launcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            local_app = base / "LocalAppData"
            appdata = base / "AppData"
            game_root = base / "Game"
            game_root.mkdir()
            (game_root / "profile-old_loadorder.json").write_text(
                json.dumps([{"id": "ModA", "isSelected": True, "isDisabled": False, "index": 0}]),
                encoding="utf-8",
            )

            docs = base / "Documents"
            launcher = docs / "Mount and Blade II Bannerlord" / "Configs" / "LauncherData.xml"
            launcher.parent.mkdir(parents=True)
            launcher.write_text(
                "<UserData><SingleplayerData><ModDatas>"
                "<UserModData><Id>ModA</Id><IsSelected>true</IsSelected></UserModData>"
                "</ModDatas></SingleplayerData></UserData>",
                encoding="utf-8",
            )

            with patch.dict("os.environ", {"APPDATA": str(appdata), "LOCALAPPDATA": str(local_app)}), patch(
                "bci_environment._documents_candidates", return_value=[docs]
            ):
                order, source, notes = discover_load_order(game_root, {"moda"})

            self.assertEqual(source, str(game_root / "profile-old_loadorder.json"))
            self.assertTrue(order["moda"]["enabled"])
            self.assertTrue(order["moda"]["profile_complete"])
            self.assertEqual(order["moda"]["profile_id"], "profile-old")
            self.assertTrue(any("выбран по единственному самому свежему файлу" in note for note in notes))

    def test_bem_saved_json_profiles_are_not_auto_detected_as_current_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            local_app = base / "LocalAppData"
            bem_root = local_app / "Bannerlord Environment Manager"
            profiles = bem_root / "profiles"
            profiles.mkdir(parents=True)
            (profiles / "saved-profile.json").write_text(json.dumps({
                "entries": [{"id": "ModA", "enabled": True, "index": 0}]
            }), encoding="utf-8")
            game_root = base / "Game"
            game_root.mkdir()

            with patch.dict("os.environ", {"LOCALAPPDATA": str(local_app)}), patch(
                "bci_environment._documents_candidates", return_value=[]
            ):
                order, source, notes = discover_load_order(game_root, {"moda"})

            self.assertEqual(order, {})
            self.assertIsNone(source)
            self.assertTrue(any("json" in note.casefold() or "соответствие" in note.casefold() for note in notes))


class CoreTests(unittest.TestCase):
    def test_indexed_xml_comparison_checks_only_shared_objects_and_preserves_write_rules(self):
        modules = [
            ModuleInfo(folder=f"Mod{name}", path="", module_id=f"Mod{name}", name=f"Mod {name}", version="1")
            for name in ("A", "B", "C", "D")
        ]
        sword_key = ("items", "item", "sword")
        shield_key = ("items", "item", "shield")

        def record(module_id: str, key: str, weight: str) -> XmlRecord:
            return XmlRecord(
                module_id=module_id,
                module_name=module_id,
                table="Items",
                tag="Item",
                key=key,
                attributes={"weight": weight, "culture": "empire"},
                source=f"{module_id}/ModuleData/items.xml",
                key_confidence="XSD",
            )

        records_by_module = {
            "ModA": {sword_key: record("ModA", "sword", "3")},
            "ModB": {sword_key: record("ModB", "sword", "5")},
            "ModC": {sword_key: record("ModC", "sword", "3")},
            "ModD": {shield_key: record("ModD", "shield", "1")},
        }
        baseline = {sword_key: {"weight": "2", "culture": "empire"}}
        stats = {
            "mod_pairs_considered": 0,
            "mod_pairs_with_shared_xml": 0,
            "shared_xml_objects_compared": 0,
        }

        pair_acc = _compare_mod_xml_records(modules, records_by_module, baseline, stats)

        self.assertEqual(set(pair_acc), {("ModA", "ModB"), ("ModA", "ModC"), ("ModB", "ModC")})
        self.assertEqual(stats["mod_pairs_considered"], 6)
        self.assertEqual(stats["mod_pairs_with_shared_xml"], 3)
        self.assertEqual(stats["shared_xml_objects_compared"], 3)
        self.assertEqual(len(pair_acc[("ModA", "ModB")]["contested"]), 1)
        self.assertEqual(pair_acc[("ModA", "ModB")]["contested"][0]["attribute"], "weight")
        self.assertEqual(pair_acc[("ModA", "ModC")]["contested"], [])
        self.assertEqual(len(pair_acc[("ModA", "ModC")]["same_write"]), 1)
        self.assertEqual(len(pair_acc[("ModB", "ModC")]["contested"]), 1)

    def test_xml_pair_comparison_does_not_flag_a_mod_that_matches_official_value(self):
        module_a = ModuleInfo(folder="A", path="", module_id="A", name="A", version="1")
        module_b = ModuleInfo(folder="B", path="", module_id="B", name="B", version="1")
        key = ("items", "item", "sword")
        records_by_module = {
            "A": {key: XmlRecord("A", "A", "Items", "Item", "sword", {"weight": "2"}, "A.xml", "XSD")},
            "B": {key: XmlRecord("B", "B", "Items", "Item", "sword", {"weight": "5"}, "B.xml", "XSD")},
        }
        stats = {"mod_pairs_considered": 0, "mod_pairs_with_shared_xml": 0, "shared_xml_objects_compared": 0}

        pair_acc = _compare_mod_xml_records([module_a, module_b], records_by_module, {key: {"weight": "2"}}, stats)

        self.assertEqual(pair_acc[("A", "B")]["contested"], [])
        self.assertEqual(pair_acc[("A", "B")]["same_write"], [])
        self.assertEqual(stats["shared_xml_objects_compared"], 1)

    def test_indexed_xml_comparison_keeps_duplicate_module_id_pair_semantics(self):
        modules = [
            ModuleInfo(folder="X1", path="", module_id="X", name="X first", version="1"),
            ModuleInfo(folder="X2", path="", module_id="X", name="X duplicate", version="1"),
            ModuleInfo(folder="Y", path="", module_id="Y", name="Y", version="1"),
        ]
        key = ("items", "item", "sword")
        records_by_module = {
            "X": {key: XmlRecord("X", "X", "Items", "Item", "sword", {"weight": "3"}, "X.xml", "XSD")},
            "Y": {key: XmlRecord("Y", "Y", "Items", "Item", "sword", {"weight": "5"}, "Y.xml", "XSD")},
        }
        stats = {"mod_pairs_considered": 0, "mod_pairs_with_shared_xml": 0, "shared_xml_objects_compared": 0}

        pair_acc = _compare_mod_xml_records(modules, records_by_module, {key: {"weight": "2"}}, stats)

        self.assertEqual(stats["mod_pairs_considered"], 3)
        self.assertEqual(stats["mod_pairs_with_shared_xml"], 3)
        self.assertEqual(stats["shared_xml_objects_compared"], 3)
        self.assertEqual(len(pair_acc[("X", "X")]["same_write"]), 1)
        self.assertEqual(len(pair_acc[("X", "Y")]["contested"]), 2)

    def test_competing_xml_attribute_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Modules"
            root.mkdir()
            write_module(root, "Native", "Native", "Native", "<Items><Item id='sword' weight='2' culture='empire'/></Items>")
            write_module(root, "ModA", "ModA", "Mod A", "<Items><Item id='sword' weight='3'/></Items>")
            write_module(root, "ModB", "ModB", "Mod B", "<Items><Item id='sword' weight='5'/></Items>")
            bem = Path(tmp) / "loadorder.json"
            bem.write_text(json.dumps({"entries": [
                {"id": "Native", "enabled": True, "index": 0, "data": {"moduleInfoExtended": {"isOfficial": True}}},
                {"id": "ModA", "enabled": True, "index": 1},
                {"id": "ModB", "enabled": True, "index": 2},
            ]}), encoding="utf-8")
            report = scan_modules(root, bem)
            finding = next(f for f in report["findings"] if f["category"] == "XML Attributes" and f["severity"] in {"High", "Medium"})
            self.assertIn("weight", json.dumps(finding["evidence"]))
            self.assertEqual(set(finding["modules"]), {"ModA", "ModB"})
            sources = json.dumps(finding["evidence"], ensure_ascii=False).replace("\\", "/")
            self.assertIn("ModuleData/items.xml", sources)
            self.assertGreaterEqual(report["performance"]["stats"]["mod_pairs_considered"], 1)
            self.assertGreaterEqual(report["performance"]["stats"]["mod_pairs_with_shared_xml"], 1)
            self.assertGreaterEqual(report["performance"]["stats"]["shared_xml_objects_compared"], 1)
            pair = next(
                item for item in report["load_order_analysis"]["file_interactions"]
                if set(item["modules"]) == {"ModA", "ModB"}
            )
            self.assertEqual(pair["xml_conflict_count"], 1)
            self.assertEqual(pair["direction_status"], "undetermined")
            self.assertIn("двух вариантов", pair["recommendation"])
            self.assertEqual(report["load_order_analysis"]["recommendation_status"], "ready_with_warnings")
            self.assertEqual(len(report["load_order_analysis"]["manual_use_warnings"]), 1)

    def test_load_order_groups_and_pairwise_coverage_reflect_declared_rules_only(self):
        dependency = ModuleInfo(
            folder="ABase", path="", module_id="Library", name="Library", version="1",
            enabled=True, enabled_known=True, load_order=1,
        )
        dependent = ModuleInfo(
            folder="ZAddon", path="", module_id="Addon", name="Addon", version="1",
            enabled=True, enabled_known=True, load_order=0,
            dependencies=[{"id": "Library", "optional": False}],
        )
        unrelated = ModuleInfo(
            folder="MIndependent", path="", module_id="Independent", name="Independent", version="1",
            enabled=True, enabled_known=True, load_order=2,
        )

        result = analyze_load_order([dependent, unrelated, dependency], "profile snapshot")
        rows = {item["module_id"]: item for item in result["recommended_order"]}

        self.assertEqual(rows["Library"]["order_group"], 1)
        self.assertEqual(rows["Independent"]["order_group"], 1)
        self.assertEqual(rows["Addon"]["order_group"], 2)
        self.assertEqual(rows["Library"]["position"], 1)
        self.assertEqual(rows["Independent"]["position"], 2)
        self.assertEqual(rows["Addon"]["position"], 3)
        evidence = result["pairwise_order_evidence"]
        self.assertEqual(evidence["total_pairs"], 3)
        self.assertEqual(evidence["determined_pairs"], 1)
        self.assertEqual(evidence["undetermined_pairs"], 2)
        self.assertEqual(evidence["coverage_pct"], 33.3)
        self.assertIn("не вероятность", evidence["interpretation"])

    def test_existing_launcher_order_does_not_steer_bci_recommendation(self):
        zulu = ModuleInfo(
            folder="Zulu", path="", module_id="Zulu", name="Zulu", version="1",
            enabled=True, enabled_known=True, load_order=0,
        )
        alpha = ModuleInfo(
            folder="Alpha", path="", module_id="Alpha", name="Alpha", version="1",
            enabled=True, enabled_known=True, load_order=1,
        )

        result = analyze_load_order([zulu, alpha], "detected launcher order")

        self.assertEqual(
            [item["module_id"] for item in result["recommended_order"]],
            ["Alpha", "Zulu"],
        )
        self.assertTrue(all(item["order_group"] == 1 for item in result["recommended_order"]))
        self.assertEqual(result["recommended_order"][0]["current_position"], 2)

    def test_load_order_text_export_labels_preview_and_does_not_claim_vortex_import(self):
        module = ModuleInfo(
            folder="Alpha", path="", module_id="Alpha", name="Alpha", version="1",
            enabled=True, enabled_known=False,
        )
        report = {
            "app_version": "0.4.0-alpha",
            "generated_at": "2026-11-24T12:00:00+00:00",
            "game_root_path": "C:/Bannerlord",
            "load_order_analysis": analyze_load_order([module]),
        }

        content = format_load_order_export(report)
        self.assertIn("ПРЕДВАРИТЕЛЬНЫЙ", content)
        self.assertIn("Alpha | Alpha", content)
        self.assertIn("не файл нативного импорта Vortex", content)
        self.assertIn("не вероятность", content)

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "order.txt"
            saved = export_load_order(report, output)
            self.assertEqual(saved, output)
            self.assertTrue(output.exists())
            self.assertIn("Alpha | Alpha", output.read_text(encoding="utf-8-sig"))

    def test_html_report_includes_own_order_engine_and_file_pair_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Modules"
            root.mkdir()
            write_module(root, "ModA", "ModA", "Mod A", "<Items><Item id='sword' weight='3'/></Items>")
            write_module(root, "ModB", "ModB", "Mod B", "<Items><Item id='sword' weight='5'/></Items>")
            profile = Path(tmp) / "profile.json"
            profile.write_text(json.dumps({"entries": [
                {"id": "ModA", "enabled": True, "index": 0},
                {"id": "ModB", "enabled": True, "index": 1},
            ]}), encoding="utf-8")

            report = scan_modules(root, profile)
            _, html_path = export_report(report, Path(tmp) / "reports")
            html_text = html_path.read_text(encoding="utf-8")

            self.assertIn("Собственная рекомендация порядка BCI", html_text)
            self.assertIn("Покрытие взаимного порядка", html_text)
            self.assertIn("Файловые взаимодействия, чувствительные к порядку", html_text)
            self.assertIn("ModA", html_text)
            self.assertIn("ModB", html_text)

    def test_missing_dependency_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Modules"
            root.mkdir()
            write_module(root, "Native", "Native", "Native", "<Items/>")
            dep = "<DependedModule Id='MissingLib'/>"
            write_module(root, "Addon", "Addon", "Addon", "<Items/>", dep)
            bem = Path(tmp) / "loadorder.json"
            bem.write_text(json.dumps({"entries": [
                {"id": "Native", "enabled": True, "index": 0, "data": {"moduleInfoExtended": {"isOfficial": True}}},
                {"id": "Addon", "enabled": True, "index": 1},
            ]}), encoding="utf-8")
            report = scan_modules(root, bem)
            self.assertTrue(any(f["category"] == "Dependencies" for f in report["findings"]))

    def test_baseline_marks_new_mod_and_candidate_impact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Modules"
            root.mkdir()
            write_module(root, "Native", "Native", "Native", "<Items/>")
            write_module(root, "ModA", "ModA", "Mod A", "<Items><Item id='sword' weight='3'/></Items>")
            bem = Path(tmp) / "loadorder.json"
            bem.write_text(json.dumps({"entries": [
                {"id": "Native", "enabled": True, "index": 0, "data": {"moduleInfoExtended": {"isOfficial": True}}},
                {"id": "ModA", "enabled": True, "index": 1},
            ]}), encoding="utf-8")
            first = scan_modules(root, bem)
            baseline_path = baseline_directory(root)
            baseline_path.parent.mkdir(parents=True, exist_ok=True)
            baseline_path.write_text(json.dumps(make_baseline(first)), encoding="utf-8")

            write_module(root, "ModB", "ModB", "Mod B", "<Items><Item id='sword' weight='7'/></Items>")
            bem.write_text(json.dumps({"entries": [
                {"id": "Native", "enabled": True, "index": 0, "data": {"moduleInfoExtended": {"isOfficial": True}}},
                {"id": "ModA", "enabled": True, "index": 1},
                {"id": "ModB", "enabled": True, "index": 2},
            ]}), encoding="utf-8")
            second = scan_modules(root, bem)
            comparison = compare_with_baseline(second)
            self.assertEqual([m["module_id"] for m in comparison["new_modules"]], ["ModB"])
            row = next(x for x in second["ranking"] if x["module_id"] == "ModB")
            self.assertEqual(row["baseline_status"], "NEW")
            self.assertGreater(row["candidate_impact"], 0)
            self.assertTrue(row["new_candidate"])

    def test_baseline_detects_load_order_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Modules"
            root.mkdir()
            write_module(root, "Native", "Native", "Native", "<Items/>")
            write_module(root, "ModA", "ModA", "Mod A", "<Items/>")
            bem = Path(tmp) / "loadorder.json"
            entries = [
                {"id": "Native", "enabled": True, "index": 0, "data": {"moduleInfoExtended": {"isOfficial": True}}},
                {"id": "ModA", "enabled": True, "index": 1},
            ]
            bem.write_text(json.dumps({"entries": entries}), encoding="utf-8")
            first = scan_modules(root, bem)
            baseline_path = baseline_directory(root)
            baseline_path.parent.mkdir(parents=True, exist_ok=True)
            baseline_path.write_text(json.dumps(make_baseline(first)), encoding="utf-8")

            entries[1]["index"] = 4
            bem.write_text(json.dumps({"entries": entries}), encoding="utf-8")
            second = scan_modules(root, bem)
            comparison = compare_with_baseline(second)
            self.assertEqual([m["module_id"] for m in comparison["changed_modules"]], ["ModA"])
            self.assertIn("load order", comparison["change_reasons"]["ModA"])



    def test_selecting_game_root_finds_modules_automatically(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Mount & Blade II Bannerlord"
            native = root / "Modules" / "Native"
            native.mkdir(parents=True)
            (native / "SubModule.xml").write_text(
                '<Module><Name value="Native"/><Id value="Native"/></Module>', encoding="utf-8"
            )
            paths = resolve_game_paths(root)
            self.assertEqual(paths.game_root, root.resolve())
            self.assertEqual(paths.modules_dir, (root / "Modules").resolve())

    def test_launcher_data_is_auto_detected_and_order_recommended(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Bannerlord"
            modules = root / "Modules"
            modules.mkdir(parents=True)
            write_module(modules, "Native", "Native", "Native", "<Items/>")
            write_module(
                modules, "Addon", "Addon", "Addon", "<Items/>",
                '<DependedModule Id="Native" Optional="false"/>'
            )
            config = root / "Configs"
            config.mkdir()
            launcher_xml = (
                '<UserData><GameType><SingleplayerData><ModDatas>'
                '<UserModData><Id>Addon</Id><IsSelected>true</IsSelected></UserModData>'
                '<UserModData><Id>Native</Id><IsSelected>true</IsSelected></UserModData>'
                '</ModDatas></SingleplayerData></GameType></UserData>'
            )
            (config / "LauncherData.xml").write_text(launcher_xml, encoding="utf-8")
            progress = []
            report = scan_modules(root, progress_callback=progress.append)
            self.assertTrue(report["enabled_state_known"])
            self.assertIn("LauncherData.xml", report["launcher_order_source"])
            self.assertEqual([x["module_id"] for x in report["load_order_analysis"]["recommended_order"]], ["Native", "Addon"])
            self.assertEqual(len(report["load_order_analysis"]["violations"]), 1)
            self.assertTrue(any(x.get("stage") == "loadorder" for x in progress))

    def test_partial_profile_keeps_unknown_modules_in_analysis_without_calling_them_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            modules = Path(tmp) / "Modules"
            modules.mkdir()
            write_module(modules, "Native", "Native", "Native", "<Items/>")
            write_module(modules, "Addon", "Addon", "Addon", "<Items/>")
            write_module(modules, "Disabled", "Disabled", "Disabled", "<Items/>")
            profile = Path(tmp) / "partial.json"
            profile.write_text(json.dumps({"entries": [
                {"id": "Native", "enabled": True, "index": 0},
                {"id": "Disabled", "enabled": False, "index": 1},
            ]}), encoding="utf-8")

            report = scan_modules(modules, profile)
            by_id = {item["module_id"]: item for item in report["modules"]}

            self.assertEqual(report["enabled_state_status"], "partial")
            self.assertFalse(report["enabled_state_known"])
            self.assertFalse(report["load_order_known"])
            self.assertEqual(report["enabled_known_modules"], 2)
            self.assertEqual(report["enabled_unknown_modules"], 1)
            self.assertEqual(report["summary"]["modules_enabled"], 2)
            self.assertEqual(report["summary"]["modules_in_analysis"], 2)
            self.assertEqual(report["summary"]["modules_confirmed_enabled"], 1)
            self.assertTrue(by_id["Addon"]["enabled"])
            self.assertFalse(by_id["Addon"]["enabled_known"])
            self.assertFalse(by_id["Disabled"]["enabled"])
            self.assertTrue(by_id["Disabled"]["enabled_known"])

            _, html_path = export_report(report, Path(tmp) / "reports")
            html_report = html_path.read_text(encoding="utf-8")
            self.assertIn("Часть состояния неизвестна", html_report)
            self.assertIn("Неизвестные по активности модули включаются в анализ консервативно", html_report)

    def test_load_order_cycle_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Modules"
            root.mkdir()
            dep_a = '<DependedModule Id="ModB" Optional="false"/>'
            dep_b = '<DependedModule Id="ModA" Optional="false"/>'
            write_module(root, "ModA", "ModA", "Mod A", "<Items/>", dep_a)
            write_module(root, "ModB", "ModB", "Mod B", "<Items/>", dep_b)
            report = scan_modules(root)
            self.assertEqual(len(report["load_order_analysis"]["cycles"]), 1)
            cycle = report["load_order_analysis"]["cycles"][0]
            self.assertEqual(set(cycle), {"ModA", "ModB"})


    def test_duplicate_module_ids_remain_visible_and_block_order_recommendation(self):
        modules = [
            ModuleInfo(folder="LibraryA", path="", module_id="SharedLib", name="Library A", version="1", enabled_known=True, load_order=0),
            ModuleInfo(folder="LibraryB", path="", module_id="SharedLib", name="Library B", version="2", enabled_known=True, load_order=1),
            ModuleInfo(folder="Addon", path="", module_id="Addon", name="Addon", version="1", enabled_known=True, load_order=2),
        ]

        result = analyze_load_order(modules, "LauncherData.xml")

        self.assertEqual(len(result["recommended_order"]), 3)
        duplicate_rows = [row for row in result["recommended_order"] if row["module_id"] == "SharedLib"]
        self.assertEqual(len(duplicate_rows), 2)
        self.assertTrue(all(row["ambiguous_identifier"] for row in duplicate_rows))
        self.assertEqual(result["recommendation_status"], "blocked_by_duplicate_ids")
        self.assertFalse(result["safe_for_manual_use"])

    def test_scan_reports_dependent_version_mismatch_in_findings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Modules"
            root.mkdir()
            write_module(root, "Library", "Library", "Library", "<Items/>")
            write_module(
                root, "Addon", "Addon", "Addon", "<Items/>",
                '<DependedModule Id="Library" DependentVersion="v2.0.0" Optional="false"/>',
            )
            profile = Path(tmp) / "loadorder.json"
            profile.write_text(json.dumps({"entries": [
                {"id": "Library", "enabled": True, "index": 0},
                {"id": "Addon", "enabled": True, "index": 1},
            ]}), encoding="utf-8")

            report = scan_modules(root, profile)

            finding = next(item for item in report["findings"] if "версия обязательной зависимости" in item["title"].casefold())
            self.assertEqual(finding["severity"], "Medium")
            self.assertEqual(finding["evidence"]["expected_version"], "v2.0.0")
            self.assertTrue(report["load_order_analysis"]["safe_for_manual_use"])
            self.assertEqual(report["load_order_analysis"]["recommendation_status"], "ready_with_warnings")

    def test_duplicate_installed_id_is_ambiguous_even_when_only_one_copy_is_enabled(self):
        modules = [
            ModuleInfo(folder="LibraryActive", path="", module_id="SharedLib", name="Active library", version="1", enabled=True, enabled_known=True, load_order=0),
            ModuleInfo(folder="LibraryDisabled", path="", module_id="SharedLib", name="Disabled library", version="2", enabled=False, enabled_known=True, load_order=1),
            ModuleInfo(folder="Addon", path="", module_id="Addon", name="Addon", version="1", enabled=True, enabled_known=True, load_order=2,
                       dependencies=[{"id": "SharedLib", "optional": False, "version": "1"}]),
        ]

        result = analyze_load_order(modules, "LauncherData.xml")

        issue = next(item for item in result["issues"] if item["kind"] == "ambiguous_module_reference")
        self.assertEqual(issue["target"], "SharedLib")
        self.assertIn("LibraryDisabled", result["ambiguous_module_ids"]["sharedlib"])
        self.assertFalse(result["safe_for_manual_use"])

    def test_dependency_to_duplicate_module_id_is_not_resolved_arbitrarily(self):
        modules = [
            ModuleInfo(folder="LibraryA", path="", module_id="SharedLib", name="Library A", version="1", enabled_known=True, load_order=0),
            ModuleInfo(folder="LibraryB", path="", module_id="SharedLib", name="Library B", version="1", enabled_known=True, load_order=1),
            ModuleInfo(folder="Addon", path="", module_id="Addon", name="Addon", version="1", enabled_known=True, load_order=2,
                       dependencies=[{"id": "SharedLib", "optional": False, "version": "1"}]),
        ]

        result = analyze_load_order(modules, "LauncherData.xml")

        issue = next(item for item in result["issues"] if item["kind"] == "ambiguous_module_reference")
        self.assertEqual(issue["target"], "SharedLib")
        self.assertFalse(result["safe_for_manual_use"])
        self.assertEqual(len(result["recommended_order"]), 3)

    def test_dependent_version_mismatch_is_a_warning_and_keeps_order_edge(self):
        dependency = ModuleInfo(
            folder="Library", path="", module_id="Library", name="Library", version="v2.0.0",
            enabled_known=True, load_order=0,
        )
        addon = ModuleInfo(
            folder="Addon", path="", module_id="Addon", name="Addon", version="1",
            enabled_known=True, load_order=1,
            dependencies=[{"id": "Library", "optional": False, "version": "v3.0.0"}],
        )

        result = analyze_load_order([dependency, addon], "LauncherData.xml")

        issue = next(item for item in result["issues"] if item["kind"] == "dependency_version_mismatch")
        self.assertEqual(issue["expected_version"], "v3.0.0")
        self.assertEqual(issue["installed_version"], "v2.0.0")
        self.assertEqual(issue["severity"], "Medium")
        self.assertEqual(result["constraints"][0]["before_id"], "Library")
        self.assertEqual(result["recommendation_status"], "ready_with_warnings")
        self.assertTrue(result["safe_for_manual_use"])

    def test_blse_metadata_version_supports_documented_wildcards(self):
        native = ModuleInfo(
            folder="Native", path="", module_id="Native", name="Native", version="e1.5.6",
            enabled_known=True, load_order=0,
        )
        addon = ModuleInfo(
            folder="Addon", path="", module_id="Addon", name="Addon", version="1",
            enabled_known=True, load_order=1,
            ordering_metadata=[{"id": "Native", "order": "LoadBeforeThis", "version": "e1.5.*"}],
        )

        result = analyze_load_order([native, addon], "LauncherData.xml")

        self.assertFalse(any(item["kind"] == "metadata_version_mismatch" for item in result["issues"]))
        self.assertEqual(result["constraints"][0]["before_id"], "Native")
        self.assertEqual(result["constraints"][0]["after_id"], "Addon")

    def test_blse_metadata_version_mismatch_is_reported_without_claiming_crash(self):
        native = ModuleInfo(
            folder="Native", path="", module_id="Native", name="Native", version="e1.5.6",
            enabled_known=True, load_order=0,
        )
        addon = ModuleInfo(
            folder="Addon", path="", module_id="Addon", name="Addon", version="1",
            enabled_known=True, load_order=1,
            ordering_metadata=[{"id": "Native", "order": "LoadBeforeThis", "version": "e1.6.0.*"}],
        )

        result = analyze_load_order([native, addon], "LauncherData.xml")

        issue = next(item for item in result["issues"] if item["kind"] == "metadata_version_mismatch")
        self.assertEqual(issue["severity"], "Medium")
        self.assertIn("ниже минимума", issue["description"])
        self.assertEqual(len(result["constraints"]), 1)

    def test_order_recommendation_is_provisional_without_confirmed_profile(self):
        module = ModuleInfo(
            folder="Addon", path="", module_id="Addon", name="Addon", version="1",
            enabled=True, enabled_known=False,
        )

        result = analyze_load_order([module])

        self.assertEqual(result["recommendation_status"], "provisional")
        self.assertFalse(result["safe_for_manual_use"])

    def test_modules_to_load_after_this_and_metadata_create_order_edges(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Modules"
            root.mkdir()
            write_module(root, "Native", "Native", "Native", "<Items/>")
            module_a = write_module(root, "ModA", "ModA", "Mod A", "<Items/>")
            (module_a / "SubModule.xml").write_text(
                '<Module><Name value="Mod A"/><Id value="ModA"/>'
                '<ModulesToLoadAfterThis><Module Id="ModB"/></ModulesToLoadAfterThis></Module>',
                encoding="utf-8",
            )
            module_b = write_module(root, "ModB", "ModB", "Mod B", "<Items/>")
            (module_b / "SubModule.xml").write_text(
                '<Module><Name value="Mod B"/><Id value="ModB"/>'
                '<DependedModuleMetadatas><DependedModuleMetadata id="Native" order="LoadBeforeThis"/></DependedModuleMetadatas>'
                '</Module>',
                encoding="utf-8",
            )
            report = scan_modules(root)
            order = [x["module_id"] for x in report["load_order_analysis"]["recommended_order"]]
            self.assertLess(order.index("Native"), order.index("ModB"))
            self.assertLess(order.index("ModA"), order.index("ModB"))

    def test_incompatibility_is_not_claimed_as_active_without_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Modules"
            root.mkdir()
            write_module(root, "ModA", "ModA", "Mod A", "<Items/>")
            write_module(root, "ModB", "ModB", "Mod B", "<Items/>")
            (root / "ModA" / "SubModule.xml").write_text(
                '<Module><Name value="Mod A"/><Id value="ModA"/>'
                '<IncompatibleModules><Module Id="ModB"/></IncompatibleModules></Module>',
                encoding="utf-8",
            )
            report = scan_modules(root)
            issue = next(x for x in report["load_order_analysis"]["issues"] if x["kind"] == "incompatible_modules")
            self.assertEqual(issue["severity"], "Medium")
            self.assertIn("активность не определена", issue["description"])

    def test_module_file_inventory_classifies_files_from_one_walk(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = Path(tmp) / "ModuleA"
            (module / "ModuleData").mkdir(parents=True)
            (module / "SubModule.xml").write_text("<Module/>", encoding="utf-8")
            (module / "ModuleData" / "items.xml").write_text("<Items/>", encoding="utf-8")
            (module / "ModuleData" / "schema.xsd").write_text("<schema/>", encoding="utf-8")
            (module / "ModuleData" / "transforms.xslt").write_text("<stylesheet/>", encoding="utf-8")
            asset_dir = module / "Assets" / "Textures"
            asset_dir.mkdir(parents=True)
            (asset_dir / "texture.dds").write_bytes(b"asset")
            (module / "ModuleData" / "readme.txt").write_text("ignored resource type", encoding="utf-8")

            inventory = _collect_module_files(module)

            self.assertEqual({p.name for p in inventory.xml_files}, {"items.xml"})
            self.assertEqual({p.name for p in inventory.xsd_files}, {"schema.xsd"})
            self.assertEqual({p.name for p in inventory.xslt_files}, {"transforms.xslt"})
            self.assertEqual({p.name for p in inventory.resource_files}, {"texture.dds"})
            self.assertEqual(len(inventory.all_files), 6)

    def test_resource_collision_skips_hashing_when_file_sizes_differ(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Modules"
            root.mkdir()
            native = write_module(root, "Native", "Native", "Native", "<Items/>")
            addon = write_module(root, "Addon", "Addon", "Addon", "<Items/>")
            native_resource = native / "Assets" / "shared.bin"
            addon_resource = addon / "Assets" / "shared.bin"
            native_resource.parent.mkdir(parents=True)
            addon_resource.parent.mkdir(parents=True)
            native_resource.write_bytes(b"short")
            addon_resource.write_bytes(b"this resource is longer")

            bem = Path(tmp) / "loadorder.json"
            bem.write_text(json.dumps({"entries": [
                {"id": "Native", "enabled": True, "index": 0, "data": {"moduleInfoExtended": {"isOfficial": True}}},
                {"id": "Addon", "enabled": True, "index": 1},
            ]}), encoding="utf-8")

            original_hash = bci_core.sha256_file
            hashed_paths = []

            def tracking_hash(path, chunk_size=1024 * 1024):
                hashed_paths.append(Path(path))
                return original_hash(path, chunk_size)

            with patch("bci_core.sha256_file", side_effect=tracking_hash):
                report = scan_modules(root, bem)

            finding = next(f for f in report["findings"] if f["category"] == "Resource Override")
            self.assertTrue(finding["evidence"]["content_differs"])
            self.assertEqual(finding["evidence"]["comparison_method"], "size")
            self.assertTrue(finding["evidence"]["comparison_complete"])
            self.assertEqual(
                {item["size_bytes"] for item in finding["evidence"]["owners"]},
                {len(b"short"), len(b"this resource is longer")},
            )
            self.assertNotIn(native_resource, hashed_paths)
            self.assertNotIn(addon_resource, hashed_paths)
            stats = report["performance"]["stats"]
            self.assertEqual(stats["resource_collision_files_hashed"], 0)
            self.assertEqual(stats["resource_collision_bytes_hashed"], 0)
            self.assertEqual(stats["resource_collision_size_mismatch_paths"], 1)
            self.assertEqual(stats["resource_collision_files_skipped_by_size"], 2)

    def test_resource_collision_hashes_equal_sized_files_to_detect_content_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Modules"
            root.mkdir()
            native = write_module(root, "Native", "Native", "Native", "<Items/>")
            addon = write_module(root, "Addon", "Addon", "Addon", "<Items/>")
            native_resource = native / "Assets" / "shared.bin"
            addon_resource = addon / "Assets" / "shared.bin"
            native_resource.parent.mkdir(parents=True)
            addon_resource.parent.mkdir(parents=True)
            native_resource.write_bytes(b"same-size-A")
            addon_resource.write_bytes(b"same-size-B")

            bem = Path(tmp) / "loadorder.json"
            bem.write_text(json.dumps({"entries": [
                {"id": "Native", "enabled": True, "index": 0, "data": {"moduleInfoExtended": {"isOfficial": True}}},
                {"id": "Addon", "enabled": True, "index": 1},
            ]}), encoding="utf-8")

            report = scan_modules(root, bem)
            finding = next(f for f in report["findings"] if f["category"] == "Resource Override")
            self.assertTrue(finding["evidence"]["content_differs"])
            self.assertEqual(finding["evidence"]["comparison_method"], "sha256")
            self.assertEqual(len({item["sha256"] for item in finding["evidence"]["owners"]}), 2)
            stats = report["performance"]["stats"]
            self.assertEqual(stats["resource_collision_files_hashed"], 2)
            self.assertEqual(stats["resource_collision_bytes_hashed"], len(b"same-size-A") * 2)
            self.assertEqual(stats["resource_collision_size_mismatch_paths"], 0)

    def test_resource_collision_marks_unreadable_equal_sized_file_as_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Modules"
            root.mkdir()
            native = write_module(root, "Native", "Native", "Native", "<Items/>")
            addon = write_module(root, "Addon", "Addon", "Addon", "<Items/>")
            native_resource = native / "Assets" / "shared.bin"
            addon_resource = addon / "Assets" / "shared.bin"
            for resource in (native_resource, addon_resource):
                resource.parent.mkdir(parents=True, exist_ok=True)
                resource.write_bytes(b"equal-size-data")

            bem = Path(tmp) / "loadorder.json"
            bem.write_text(json.dumps({"entries": [
                {"id": "Native", "enabled": True, "index": 0, "data": {"moduleInfoExtended": {"isOfficial": True}}},
                {"id": "Addon", "enabled": True, "index": 1},
            ]}), encoding="utf-8")

            original_hash = bci_core.sha256_file
            target_resource = bci_core._normalized_file_path(addon_resource)
            failed_resource_reads = []

            def fail_addon_resource(path, chunk_size=1024 * 1024):
                if bci_core._normalized_file_path(Path(path)) == target_resource:
                    failed_resource_reads.append(str(path))
                    raise OSError("simulated unreadable resource")
                return original_hash(path, chunk_size)

            with patch("bci_core.sha256_file", side_effect=fail_addon_resource):
                report = scan_modules(root, bem)

            self.assertEqual(len(failed_resource_reads), 1)
            finding = next(f for f in report["findings"] if f["category"] == "Resource Override")
            self.assertFalse(finding["evidence"]["content_differs"])
            self.assertFalse(finding["evidence"]["comparison_complete"])
            self.assertEqual(finding["evidence"]["comparison_method"], "incomplete")
            self.assertEqual(finding["confidence"], "Low")
            self.assertIn("не удалось полностью сравнить", finding["description"].lower())
            self.assertNotIn("содержат идентичный ресурс", finding["description"].lower())

    def test_resource_collision_keeps_identical_equal_sized_files_informational(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Modules"
            root.mkdir()
            native = write_module(root, "Native", "Native", "Native", "<Items/>")
            addon = write_module(root, "Addon", "Addon", "Addon", "<Items/>")
            for module_dir in (native, addon):
                resource = module_dir / "Assets" / "shared.bin"
                resource.parent.mkdir(parents=True)
                resource.write_bytes(b"identical-content")

            bem = Path(tmp) / "loadorder.json"
            bem.write_text(json.dumps({"entries": [
                {"id": "Native", "enabled": True, "index": 0, "data": {"moduleInfoExtended": {"isOfficial": True}}},
                {"id": "Addon", "enabled": True, "index": 1},
            ]}), encoding="utf-8")

            report = scan_modules(root, bem)
            finding = next(f for f in report["findings"] if f["category"] == "Resource Override")
            self.assertFalse(finding["evidence"]["content_differs"])
            self.assertEqual(finding["evidence"]["comparison_method"], "sha256")
            self.assertTrue(finding["evidence"]["comparison_complete"])
            self.assertEqual(finding["severity"], "Info")
            stats = report["performance"]["stats"]
            self.assertEqual(stats["resource_collision_files_hashed"], 2)
            self.assertEqual(stats["resource_collision_bytes_hashed"], len(b"identical-content") * 2)

    def test_scan_report_includes_performance_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Modules"
            root.mkdir()
            write_module(root, "Native", "Native", "Native", "<Items><Item id='sword'/></Items>")
            report = scan_modules(root)

            performance = report["performance"]
            self.assertGreaterEqual(performance["scan_total_ms"], 0)
            self.assertGreaterEqual(performance["stats"]["files_discovered"], 2)
            self.assertGreaterEqual(performance["stats"]["xml_files_parsed"], 1)
            self.assertIn("fingerprint_ms", performance["stages_ms"])
            self.assertIn("module_analysis_ms", performance["stages_ms"])
            self.assertIn("xml_extract_ms", performance["stages_ms"])
            detailed_stages = (
                "duplicate_module_checks_ms",
                "load_order_graph_ms",
                "load_order_findings_ms",
                "xml_record_index_ms",
                "official_xml_baseline_ms",
                "official_override_scan_ms",
                "xml_pair_comparison_ms",
                "xml_parse_error_findings_ms",
                "resource_collision_analysis_ms",
                "xslt_findings_ms",
                "ranking_ms",
                "report_data_assembly_ms",
            )
            for stage in detailed_stages:
                self.assertIn(stage, performance["stages_ms"])
                self.assertGreaterEqual(performance["stages_ms"][stage], 0)
            self.assertGreaterEqual(performance["stats"]["xml_records_indexed"], 1)
            self.assertIn("resource_collision_hash_ms", performance["stages_ms"])
            self.assertIn("resource_collision_files_hashed", performance["stats"])
            json_path, html_path = export_report(report, Path(tmp) / "BCI_Reports")
            self.assertTrue(json_path.is_file())
            self.assertTrue(html_path.is_file())
            html_text = html_path.read_text(encoding="utf-8")
            self.assertIn("Производительность сканирования", html_text)
            self.assertIn("Сравнение пар модов по XML", html_text)
            self.assertIn("XML-записей проиндексировано", html_text)
            self.assertIn("Пар с общими XML-объектами", html_text)
            self.assertIn("Ресурсных файлов захешировано", html_text)
            self.assertIn("Путей с разным размером", html_text)
            self.assertIn("SHA-256 ресурсов одинакового размера", html_text)
            self.assertIn("Строки со знаком", html_text)


if __name__ == "__main__":
    unittest.main()
