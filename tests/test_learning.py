from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bci_ml import FEATURE_NAMES, export_active_model_package, feature_vector, import_model_package, predict_report, train_model
from bci_store import BCIStore


class LearningPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = BCIStore(Path(self.temp.name) / "bci.db")

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def make_report(index: int, regression: bool) -> dict:
        modules_count = 12 + (index % 7) + (5 if regression else 0)
        modules = []
        for number in range(modules_count):
            modules.append({
                "module_id": f"Mod_{index}_{number}",
                "version": "1.0.0",
                "fingerprint": f"{index:02d}-{number:02d}-{'bad' if regression else 'ok'}",
                "enabled": True,
                "enabled_known": True,
                "load_order": number,
                "is_official": number < 5,
                "dlls": ["bin/Test.dll"] if number % 2 else [],
                "xml_files": ["ModuleData/test.xml"] * (1 + (index % 3)),
                "xslt_files": [],
                "resource_files": [],
            })
        findings = []
        if regression:
            findings = [
                {"category": "XML Attributes", "severity": "High"},
                {"category": "Load Order", "severity": "Medium"},
                {"category": "XML Parse", "severity": "Critical"},
            ]
        return {
            "generated_at": f"2026-11-23T12:{index:02d}:00+00:00",
            "game_root_path": f"C:/Games/Bannerlord/Test{index}",
            "game_executable": "Bannerlord.exe",
            "load_order_known": True,
            "modules": modules,
            "findings": findings,
            "summary": {
                "modules_found": modules_count,
                "modules_enabled": modules_count,
                "findings_total": len(findings),
                "critical": 1 if regression else 0,
                "high": 1 if regression else 0,
                "medium": 1 if regression else 0,
                "info": 0,
                "xml_records": 30 + index,
                "xsd_unique_rules": 4,
                "load_order_constraints": 8,
                "load_order_violations": 2 if regression else 0,
                "load_order_cycles": 1 if regression else 0,
            },
            "baseline_comparison": {"candidate_count": 2 if regression else 0},
            "load_order_analysis": {"constraints": [1] * 8, "violations": [1, 2] if regression else [], "cycles": [1] if regression else []},
        }

    def test_report_is_saved_locally_and_labelled(self):
        report = self.make_report(1, False)
        scan_id = self.store.record_scan(report)
        self.assertEqual(scan_id, report["scan_id"])
        self.store.label_scan(scan_id, "stable", "Запуск игры", "До главного меню дошло")
        stats = self.store.get_stats()
        self.assertEqual(stats["scans"], 1)
        self.assertEqual(stats["labelled"], 1)
        self.assertEqual(stats["stable"], 1)
        rows = self.store.get_training_records()
        self.assertEqual(rows[0]["outcome"], "stable")
        self.assertEqual(rows[0]["scenario"], "Запуск игры")

    def test_feature_schema_is_stable(self):
        vector = feature_vector(self.make_report(2, True), "Обычное сражение")
        self.assertEqual(len(vector), len(FEATURE_NAMES))
        self.assertGreater(vector[4], 0)  # critical findings
        self.assertEqual(vector[-3], 1.0)  # battle scenario flag

    def test_local_lightgbm_training_and_registration(self):
        for index in range(24):
            is_regression = index % 2 == 1
            report = self.make_report(index, is_regression)
            scan_id = self.store.record_scan(report)
            outcome = "regression" if is_regression else "stable"
            self.store.label_scan(scan_id, outcome, "Запуск игры", "Controlled test fixture")
        manifest = train_model(self.store)
        active = self.store.active_model()
        self.assertIsNotNone(active)
        self.assertEqual(active["model_id"], manifest["model_id"])
        self.assertTrue(Path(active["model_path"]).is_file())
        self.assertEqual(manifest["model_scope"], "assembly_regression_shadow")
        export_path = Path(self.temp.name) / "transfer.bci-model.zip"
        export_active_model_package(self.store, export_path)
        receiver = BCIStore(Path(self.temp.name) / "receiver" / "bci.db")
        imported = import_model_package(receiver, export_path)
        self.assertEqual(imported["feature_schema_version"], manifest["feature_schema_version"])
        prediction = predict_report(receiver, self.make_report(99, False), "Запуск игры")
        self.assertIsNotNone(prediction)
        self.assertIn(prediction["class"], {"stable", "regression"})


if __name__ == "__main__":
    unittest.main()
