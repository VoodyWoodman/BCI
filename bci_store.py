from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

APP_DATA_NAME = "BCI"
VALID_OUTCOMES = {"stable", "regression", "unknown"}


def app_data_dir() -> Path:
    """Return a per-user writable data directory; never write into Program Files."""
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    else:
        root = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    path = root / APP_DATA_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


class BCIStore:
    """Local-first SQLite storage for scans, manual test labels and model versions."""

    def __init__(self, database_path: str | Path | None = None) -> None:
        self.root = Path(database_path).parent if database_path else app_data_dir()
        self.root.mkdir(parents=True, exist_ok=True)
        self.database_path = Path(database_path) if database_path else self.root / "bci.db"
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.models_dir = self.root / "Models"
        self.models_dir.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS scans (
                    scan_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    game_path_hash TEXT NOT NULL,
                    build_signature TEXT NOT NULL,
                    modules_count INTEGER NOT NULL,
                    findings_count INTEGER NOT NULL,
                    outcome TEXT NOT NULL DEFAULT 'unknown',
                    scenario TEXT NOT NULL DEFAULT '',
                    test_notes TEXT NOT NULL DEFAULT '',
                    report_json TEXT NOT NULL
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS model_versions (
                    model_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    model_path TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 0
                )
            """)
            # Additive, backward-compatible cache table. Existing scan/model data
            # and the database schema used by prior BCI versions remain intact.
            db.execute("""
                CREATE TABLE IF NOT EXISTS file_hash_cache (
                    path_norm TEXT PRIMARY KEY,
                    size_bytes INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    sha256 TEXT NOT NULL
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS idx_scans_created ON scans(created_at DESC)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_scans_training ON scans(outcome, build_signature)")

    def get_file_hash_cache(self) -> dict[str, tuple[int, int, str]]:
        """Return cached file digests keyed by normalized absolute path."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT path_norm, size_bytes, mtime_ns, sha256 FROM file_hash_cache"
            ).fetchall()
        return {
            str(row["path_norm"]): (int(row["size_bytes"]), int(row["mtime_ns"]), str(row["sha256"]))
            for row in rows
        }

    def upsert_file_hash_cache(
        self, entries: dict[str, tuple[int, int, str]]
    ) -> int:
        """Persist changed hashes in one transaction; return the number written."""
        if not entries:
            return 0
        rows = [
            (path_norm, int(values[0]), int(values[1]), str(values[2]))
            for path_norm, values in entries.items()
            if path_norm and values[2]
        ]
        if not rows:
            return 0
        with self._connect() as db:
            db.executemany(
                """
                INSERT INTO file_hash_cache (path_norm, size_bytes, mtime_ns, sha256)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(path_norm) DO UPDATE SET
                    size_bytes=excluded.size_bytes,
                    mtime_ns=excluded.mtime_ns,
                    sha256=excluded.sha256
                """,
                rows,
            )
        return len(rows)

    def clear_file_hash_cache(self) -> int:
        """Clear only the performance cache, preserving all scan/model records."""
        with self._connect() as db:
            cursor = db.execute("DELETE FROM file_hash_cache")
            return int(cursor.rowcount)

    @staticmethod
    def build_signature(report: dict[str, Any]) -> str:
        modules = []
        for item in report.get("modules", []):
            modules.append({
                "id": str(item.get("module_id", "")).casefold(),
                "version": str(item.get("version", "")),
                "fingerprint": str(item.get("fingerprint", "")),
                "enabled": item.get("enabled") if item.get("enabled_known") else None,
                "order": item.get("load_order") if report.get("load_order_known") else None,
            })
        payload = {
            "game_executable": Path(str(report.get("game_executable") or "")).name.casefold(),
            "modules": sorted(modules, key=lambda row: row["id"]),
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def record_scan(self, report: dict[str, Any]) -> str:
        scan_id = str(uuid.uuid4())
        created_at = str(report.get("generated_at") or datetime.now(timezone.utc).isoformat())
        game_path = str(report.get("game_root_path") or "")
        game_path_hash = hashlib.sha256(game_path.casefold().encode("utf-8")).hexdigest()
        signature = self.build_signature(report)
        summary = report.get("summary", {})
        report["scan_id"] = scan_id
        report["collection"] = {
            "stored_locally": True,
            "database": str(self.database_path),
            "note": "Отчёт сохранён локально. Автоматическая отправка данных отключена.",
        }
        payload = json.dumps(report, ensure_ascii=False, separators=(",", ":"))
        with self._connect() as db:
            db.execute("""
                INSERT INTO scans (
                    scan_id, created_at, game_path_hash, build_signature,
                    modules_count, findings_count, outcome, scenario, test_notes, report_json
                ) VALUES (?, ?, ?, ?, ?, ?, 'unknown', '', '', ?)
            """, (
                scan_id, created_at, game_path_hash, signature,
                int(summary.get("modules_found", len(report.get("modules", [])))),
                int(summary.get("findings_total", len(report.get("findings", [])))),
                payload,
            ))
        return scan_id

    def label_scan(self, scan_id: str, outcome: str, scenario: str, notes: str = "") -> None:
        if outcome not in VALID_OUTCOMES:
            raise ValueError(f"Неизвестная метка результата: {outcome}")
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE scans SET outcome=?, scenario=?, test_notes=? WHERE scan_id=?",
                (outcome, scenario.strip(), notes.strip(), scan_id),
            )
            if cursor.rowcount != 1:
                raise KeyError("Отчёт не найден в локальной базе BCI.")

    def list_scans(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("""
                SELECT scan_id, created_at, modules_count, findings_count, outcome, scenario, test_notes
                FROM scans ORDER BY created_at DESC LIMIT ?
            """, (max(1, min(limit, 1000)),)).fetchall()
        return [dict(row) for row in rows]

    def get_stats(self) -> dict[str, int]:
        with self._connect() as db:
            row = db.execute("""
                SELECT COUNT(*) AS scans,
                       SUM(CASE WHEN outcome IN ('stable','regression') THEN 1 ELSE 0 END) AS labelled,
                       SUM(CASE WHEN outcome='stable' THEN 1 ELSE 0 END) AS stable,
                       SUM(CASE WHEN outcome='regression' THEN 1 ELSE 0 END) AS regression,
                       COUNT(DISTINCT CASE WHEN outcome IN ('stable','regression') THEN build_signature END) AS builds
                FROM scans
            """).fetchone()
        return {key: int(row[key] or 0) for key in ("scans", "labelled", "stable", "regression", "builds")}

    def get_training_records(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("""
                SELECT scan_id, build_signature, outcome, scenario, report_json, created_at
                FROM scans WHERE outcome IN ('stable','regression')
                ORDER BY created_at ASC
            """).fetchall()
        result = []
        for row in rows:
            try:
                report = json.loads(row["report_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            result.append({
                "scan_id": row["scan_id"],
                "build_signature": row["build_signature"],
                "outcome": row["outcome"],
                "scenario": row["scenario"],
                "report": report,
                "created_at": row["created_at"],
            })
        return result

    def register_model(self, model_id: str, model_path: Path, manifest: dict[str, Any]) -> None:
        manifest_json = json.dumps(manifest, ensure_ascii=False, separators=(",", ":"))
        with self._connect() as db:
            db.execute("UPDATE model_versions SET active=0")
            db.execute("""
                INSERT OR REPLACE INTO model_versions (model_id, created_at, model_path, manifest_json, active)
                VALUES (?, ?, ?, ?, 1)
            """, (model_id, datetime.now(timezone.utc).isoformat(), str(model_path), manifest_json))

    def active_model(self) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("""
                SELECT model_id, created_at, model_path, manifest_json
                FROM model_versions WHERE active=1 ORDER BY created_at DESC LIMIT 1
            """).fetchone()
        if not row:
            return None
        try:
            manifest = json.loads(row["manifest_json"])
        except json.JSONDecodeError:
            manifest = {}
        return {
            "model_id": row["model_id"],
            "created_at": row["created_at"],
            "model_path": row["model_path"],
            "manifest": manifest,
        }

    def previous_models(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("""
                SELECT model_id, created_at, model_path, active FROM model_versions
                ORDER BY created_at DESC LIMIT 20
            """).fetchall()
        return [dict(row) for row in rows]
