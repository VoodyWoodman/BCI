from __future__ import annotations

import hashlib
import json
import random
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from bci_store import BCIStore

FEATURE_SCHEMA_VERSION = 1
SCENARIOS = [
    "Запуск игры",
    "Загрузка сохранения",
    "Новая кампания",
    "Карта кампании",
    "Обычное сражение",
    "Морское сражение",
    "Другое",
]
LABEL_TO_INT = {"stable": 0, "regression": 1}
FEATURE_NAMES = [
    "modules_found", "modules_enabled", "third_party_modules", "findings_total",
    "critical", "high", "medium", "info", "xml_records", "xsd_unique_rules",
    "load_order_constraints", "load_order_violations", "load_order_cycles",
    "baseline_candidates", "dll_count", "xml_file_count", "xslt_file_count", "resource_file_count",
    "manifest_findings", "load_order_findings", "xml_attribute_findings", "xml_parse_findings",
    "resource_override_findings", "xslt_findings", "official_override_findings",
    "scenario_launch", "scenario_save", "scenario_campaign", "scenario_map",
    "scenario_battle", "scenario_naval", "scenario_other",
]


def feature_vector(report: dict[str, Any], scenario: str) -> list[float]:
    summary = report.get("summary", {})
    modules = report.get("modules", [])
    findings = report.get("findings", [])
    cats = defaultdict(int)
    for item in findings:
        cats[str(item.get("category", "")).casefold()] += 1
    scenario_norm = scenario.strip().casefold()
    scenario_flags = [
        float(scenario_norm == "запуск игры".casefold()),
        float(scenario_norm == "загрузка сохранения".casefold()),
        float(scenario_norm == "новая кампания".casefold()),
        float(scenario_norm == "карта кампании".casefold()),
        float(scenario_norm == "обычное сражение".casefold()),
        float(scenario_norm == "морское сражение".casefold()),
        float(scenario_norm not in {x.casefold() for x in SCENARIOS[:-1]}),
    ]
    baseline = report.get("baseline_comparison", {})
    order = report.get("load_order_analysis", {})
    vector = [
        float(summary.get("modules_found", len(modules))),
        float(summary.get("modules_enabled", sum(1 for m in modules if m.get("enabled")))),
        float(sum(1 for m in modules if not m.get("is_official"))),
        float(summary.get("findings_total", len(findings))),
        float(summary.get("critical", 0)),
        float(summary.get("high", 0)),
        float(summary.get("medium", 0)),
        float(summary.get("info", 0)),
        float(summary.get("xml_records", 0)),
        float(summary.get("xsd_unique_rules", 0)),
        float(summary.get("load_order_constraints", len(order.get("constraints", [])))),
        float(summary.get("load_order_violations", len(order.get("violations", [])))),
        float(summary.get("load_order_cycles", len(order.get("cycles", [])))),
        float(baseline.get("candidate_count", 0)),
        float(sum(len(m.get("dlls", [])) for m in modules)),
        float(sum(len(m.get("xml_files", [])) for m in modules)),
        float(sum(len(m.get("xslt_files", [])) for m in modules)),
        float(sum(len(m.get("resource_files", [])) for m in modules)),
        float(cats["manifest"]),
        float(cats["load order"]),
        float(cats["xml attributes"]),
        float(cats["xml parse"]),
        float(cats["resource override"]),
        float(cats["xslt"]),
        float(cats["official data override"]),
        *scenario_flags,
    ]
    if len(vector) != len(FEATURE_NAMES):
        raise RuntimeError("BCI feature schema mismatch.")
    return vector


def _make_training_rows(records: list[dict[str, Any]]) -> tuple[list[list[float]], list[int], list[str], int]:
    # Keep only one observation per exact mod-set + scenario. Conflicting labels
    # for the same group are excluded rather than silently choosing one.
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in records:
        scenario = row.get("scenario") or "Другое"
        grouped[(row["build_signature"], scenario)].append(row)

    vectors: list[list[float]] = []
    labels: list[int] = []
    groups: list[str] = []
    excluded_conflicts = 0
    for (signature, scenario), observations in grouped.items():
        observed_labels = {item["outcome"] for item in observations}
        if len(observed_labels) != 1:
            excluded_conflicts += 1
            continue
        outcome = next(iter(observed_labels))
        representative = observations[-1]
        vectors.append(feature_vector(representative["report"], scenario))
        labels.append(LABEL_TO_INT[outcome])
        groups.append(signature)
    return vectors, labels, groups, excluded_conflicts


def _group_split(labels: list[int], groups: list[str], test_fraction: float = 0.25):
    group_indices: dict[str, list[int]] = defaultdict(list)
    for index, group in enumerate(groups):
        group_indices[group].append(index)
    unique_groups = list(group_indices)
    if len(unique_groups) < 4:
        return None
    for seed in range(500):
        rng = random.Random(20261123 + seed)
        shuffled = unique_groups[:]
        rng.shuffle(shuffled)
        test_count = max(2, int(round(len(shuffled) * test_fraction)))
        test_groups = set(shuffled[:test_count])
        train_indices = [i for group, indices in group_indices.items() if group not in test_groups for i in indices]
        test_indices = [i for group, indices in group_indices.items() if group in test_groups for i in indices]
        if not train_indices or not test_indices:
            continue
        if len({labels[i] for i in train_indices}) == 2 and len({labels[i] for i in test_indices}) == 2:
            if len(train_indices) >= 6 and len(test_indices) >= 4:
                return train_indices, test_indices
    return None


def train_model(store: BCIStore) -> dict[str, Any]:
    try:
        import lightgbm as lgb
    except Exception as exc:
        raise RuntimeError(f"LightGBM не загружен: {exc}") from exc

    records = store.get_training_records()
    X, y, groups, excluded_conflicts = _make_training_rows(records)
    stable = sum(1 for value in y if value == 0)
    regression = sum(1 for value in y if value == 1)
    unique_builds = len(set(groups))
    if len(y) < 16 or min(stable, regression) < 5 or unique_builds < 6:
        raise ValueError(
            "Пока недостаточно независимых подтверждённых данных для обучения. "
            f"Сейчас: {len(y)} примеров, без регрессии {stable}, с регрессией {regression}, "
            f"разных сборок {unique_builds}. Нужно не менее 16 примеров, по 5 каждого класса и 6 разных сборок."
        )

    split = _group_split(y, groups)
    if split is None:
        raise ValueError(
            "Не удалось сформировать честную контрольную выборку с разделением по сборкам. "
            "Нужны разные наборы модов как для успешных, так и для проблемных тестов."
        )
    train_idx, test_idx = split
    x_train = np.asarray([X[i] for i in train_idx], dtype=np.float32)
    y_train = np.asarray([y[i] for i in train_idx], dtype=np.int32)
    x_test = np.asarray([X[i] for i in test_idx], dtype=np.float32)
    y_test = np.asarray([y[i] for i in test_idx], dtype=np.int32)

    params = {
        "objective": "binary",
        "metric": ["binary_logloss", "auc"],
        "learning_rate": 0.04,
        "num_leaves": 7,
        "max_depth": 3,
        "min_data_in_leaf": 3,
        "feature_fraction": 0.9,
        "lambda_l2": 2.0,
        "verbosity": -1,
        "seed": 42,
        "num_threads": max(1, min(4, os_cpu_count())),
    }
    train_set = lgb.Dataset(x_train, label=y_train, feature_name=FEATURE_NAMES)
    valid_set = lgb.Dataset(x_test, label=y_test, reference=train_set, feature_name=FEATURE_NAMES)
    callbacks = [lgb.log_evaluation(period=0)]
    if len(test_idx) >= 6:
        callbacks.append(lgb.early_stopping(12, verbose=False))
    validation_model = lgb.train(
        params, train_set, num_boost_round=100,
        valid_sets=[valid_set], valid_names=["holdout"], callbacks=callbacks,
    )
    probabilities = np.asarray(validation_model.predict(x_test), dtype=float)
    predicted = (probabilities >= 0.5).astype(np.int32)
    accuracy = float(np.mean(predicted == y_test))
    true_positive = int(np.sum((predicted == 1) & (y_test == 1)))
    false_positive = int(np.sum((predicted == 1) & (y_test == 0)))
    false_negative = int(np.sum((predicted == 0) & (y_test == 1)))
    precision = true_positive / max(1, true_positive + false_positive)
    recall = true_positive / max(1, true_positive + false_negative)
    f1 = 2 * precision * recall / max(1e-9, precision + recall)
    positives = probabilities[y_test == 1]
    negatives = probabilities[y_test == 0]
    if len(positives) and len(negatives):
        auc = float(sum((p > n) + 0.5 * (p == n) for p in positives for n in negatives) / (len(positives) * len(negatives)))
    else:
        auc = None

    best_rounds = int(validation_model.best_iteration or 60)
    full_set = lgb.Dataset(np.asarray(X, dtype=np.float32), label=np.asarray(y, dtype=np.int32), feature_name=FEATURE_NAMES)
    final_model = lgb.train(params, full_set, num_boost_round=max(20, best_rounds))

    model_id = datetime.now(timezone.utc).strftime("0.1.%Y%m%d%H%M%S%f")
    model_dir = store.models_dir / model_id
    model_dir.mkdir(parents=True, exist_ok=False)
    model_path = model_dir / "model.txt"
    final_model.save_model(str(model_path))
    digest = hashlib.sha256(model_path.read_bytes()).hexdigest()
    manifest: dict[str, Any] = {
        "model_id": model_id,
        "model_type": "LightGBM Gradient Boosted Trees",
        "lightgbm_version": getattr(lgb, "__version__", "unknown"),
        "model_scope": "assembly_regression_shadow",
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "feature_names": FEATURE_NAMES,
        "labels": {"0": "stable", "1": "regression"},
        "threshold": 0.5,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "training_examples": len(y),
        "unique_builds": unique_builds,
        "stable_examples": stable,
        "regression_examples": regression,
        "excluded_conflicting_groups": excluded_conflicts,
        "holdout": {
            "method": "group holdout by exact mod-set signature",
            "train_examples": len(train_idx),
            "test_examples": len(test_idx),
            "train_builds": len({groups[i] for i in train_idx}),
            "test_builds": len({groups[i] for i in test_idx}),
            "accuracy": accuracy,
            "precision_regression": precision,
            "recall_regression": recall,
            "f1_regression": f1,
            "auc": auc,
            "false_positives": false_positive,
            "false_negatives": false_negative,
        },
        "model_sha256": digest,
        "disclaimer": (
            "Экспериментальная оценка результата игрового теста для всей сборки. "
            "Не определяет виновный мод, не является вероятностью краша и не влияет на Risk Index."
        ),
    }
    (model_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    store.register_model(model_id, model_path, manifest)
    return manifest


def os_cpu_count() -> int:
    import os
    return os.cpu_count() or 1


def predict_report(store: BCIStore, report: dict[str, Any], scenario: str = "Запуск игры") -> dict[str, Any] | None:
    active = store.active_model()
    if not active:
        return None
    model_path = Path(active["model_path"])
    if not model_path.is_file():
        return None
    try:
        import lightgbm as lgb
        model = lgb.Booster(model_file=str(model_path))
        vector = np.asarray([feature_vector(report, scenario)], dtype=np.float32)
        probability = float(model.predict(vector)[0])
    except Exception:
        return None
    return {
        "model_id": active["model_id"],
        "scenario": scenario,
        "regression_score": probability,
        "class": "regression" if probability >= 0.5 else "stable",
        "mode": "shadow",
        "notice": "Оценка всей сборки для выбранного сценария; не устанавливает виновный мод и не меняет рекомендации.",
    }


def export_active_model_package(store: BCIStore, destination: str | Path) -> Path:
    import zipfile

    active = store.active_model()
    if not active:
        raise ValueError("Нет обученной модели для экспорта.")
    model_path = Path(active["model_path"])
    manifest_path = model_path.parent / "manifest.json"
    if not model_path.is_file() or not manifest_path.is_file():
        raise FileNotFoundError("Файлы активной модели не найдены.")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.write(model_path, arcname="model.txt")
        archive.write(manifest_path, arcname="manifest.json")
    return destination


def import_model_package(store: BCIStore, package_path: str | Path) -> dict[str, Any]:
    import re
    import shutil
    import zipfile

    package_path = Path(package_path)
    if not package_path.is_file() or package_path.stat().st_size > 250 * 1024 * 1024:
        raise ValueError("Файл модели отсутствует или превышает допустимый размер 250 МБ.")
    try:
        with zipfile.ZipFile(package_path, "r") as archive:
            entries = archive.infolist()
            names = [item.filename for item in entries]
            if len(names) != 2 or set(names) != {"model.txt", "manifest.json"}:
                raise ValueError("Пакет должен содержать только model.txt и manifest.json.")
            sizes = {item.filename: item.file_size for item in entries}
            if sizes.get("manifest.json", 0) > 1_000_000 or sizes.get("model.txt", 0) > 250 * 1024 * 1024:
                raise ValueError("Внутренний размер файлов пакета превышает допустимый предел.")
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            model_bytes = archive.read("model.txt")
    except (zipfile.BadZipFile, UnicodeDecodeError, json.JSONDecodeError, KeyError) as exc:
        raise ValueError(f"Не удалось прочитать пакет модели: {exc}") from exc

    if not isinstance(manifest, dict):
        raise ValueError("Некорректный manifest.json в пакете модели.")
    if manifest.get("feature_schema_version") != FEATURE_SCHEMA_VERSION:
        raise ValueError("Модель создана для другой версии схемы признаков. Сначала обнови BCI.")
    if manifest.get("feature_names") != FEATURE_NAMES:
        raise ValueError("Набор признаков модели не соответствует этой версии BCI.")
    if manifest.get("model_scope") != "assembly_regression_shadow":
        raise ValueError("Неизвестный тип модели. Пакет отклонён.")
    expected_hash = str(manifest.get("model_sha256") or "")
    actual_hash = hashlib.sha256(model_bytes).hexdigest()
    if not expected_hash or actual_hash.casefold() != expected_hash.casefold():
        raise ValueError("Контрольная сумма модели не совпадает. Пакет повреждён или изменён.")

    model_id_raw = str(manifest.get("model_id") or "imported-model")
    model_id = re.sub(r"[^A-Za-z0-9._-]", "_", model_id_raw)[:64] or "imported-model"
    model_dir = store.models_dir / model_id
    suffix = 1
    while model_dir.exists():
        model_dir = store.models_dir / f"{model_id}-import{suffix}"
        suffix += 1
    model_dir.mkdir(parents=True, exist_ok=False)
    model_path = model_dir / "model.txt"
    model_path.write_bytes(model_bytes)
    (model_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        import lightgbm as lgb
        lgb.Booster(model_file=str(model_path))
    except Exception as exc:
        shutil.rmtree(model_dir, ignore_errors=True)
        raise ValueError(f"LightGBM не смог загрузить модель: {exc}") from exc
    store.register_model(model_dir.name, model_path, manifest)
    return manifest
