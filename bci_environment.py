from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class GamePaths:
    game_root: Path
    modules_dir: Path
    executable: Path | None
    selected_was_modules: bool = False


def _has_bannerlord_modules(path: Path) -> bool:
    modules = path / "Modules"
    if (modules / "Native" / "SubModule.xml").is_file():
        return True
    try:
        return any(p.is_dir() and (p / "SubModule.xml").is_file() for p in modules.iterdir())
    except OSError:
        return False


def resolve_game_paths(selected_path: str | Path) -> GamePaths:
    """Accept the game root; also normalize an accidentally selected Modules/bin folder."""
    selected = Path(selected_path).expanduser().resolve()
    if not selected.exists() or not selected.is_dir():
        raise ValueError(f"Указанная папка не существует или не является каталогом:\n{selected}")

    candidates: list[tuple[Path, bool]] = []
    if selected.name.casefold() == "modules":
        candidates.append((selected.parent, True))
    else:
        candidates.append((selected, False))
        for parent in list(selected.parents)[:3]:
            candidates.append((parent, False))

    for root, was_modules in candidates:
        modules = root / "Modules"
        if modules.is_dir() and _has_bannerlord_modules(root):
            exe_candidates = [
                root / "bin" / "Win64_Shipping_Client" / "Bannerlord.exe",
                root / "bin" / "Win64_Shipping_Client" / "Bannerlord.Native.exe",
            ]
            exe = next((p for p in exe_candidates if p.is_file()), None)
            return GamePaths(root, modules, exe, was_modules)

    raise ValueError(
        "BCI не нашёл папку Modules с модулем Native.\n\n"
        "Выбери корневую папку игры Mount & Blade II Bannerlord — "
        "ту, внутри которой находятся папки Modules и bin.\n\n"
        "Пример: ...\\steamapps\\common\\Mount & Blade II Bannerlord"
    )


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _value(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return str(element.attrib.get("value") or element.attrib.get("id") or (element.text or "")).strip()


def parse_optional_bool(value: Any) -> bool | None:
    """Parse an explicit boolean without inventing a default for missing/invalid values."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    return None


def parse_launcher_data(path: Path) -> dict[str, dict[str, Any]]:
    """Read vanilla Launcher's SingleplayerData/ModDatas/UserModData collection."""
    try:
        if path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("LauncherData.xml слишком большой для безопасного чтения.")
        raw = path.read_bytes()
        if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
            raise ValueError("DOCTYPE/ENTITY запрещены в LauncherData.xml.")
        root = ET.fromstring(raw)
    except Exception as exc:
        raise ValueError(f"Не удалось прочитать {path}: {exc}") from exc

    singleplayer = next((x for x in root.iter() if _local_name(x.tag).casefold() == "singleplayerdata"), None)
    if singleplayer is None:
        singleplayer = root
    mod_datas = next((x for x in singleplayer.iter() if _local_name(x.tag).casefold() == "moddatas"), None)
    if mod_datas is None:
        raise ValueError("В LauncherData.xml не найдена секция SingleplayerData/ModDatas.")

    result: dict[str, dict[str, Any]] = {}
    index = 0
    for node in list(mod_datas):
        if _local_name(node.tag).casefold() not in {"usermoddata", "moddata", "module", "user-module-data"}:
            continue
        module_id = ""
        selected_value: Any = None
        for child in list(node):
            child_name = _local_name(child.tag).casefold()
            if child_name == "id":
                module_id = _value(child)
            elif child_name in {"isselected", "selected", "enabled"}:
                selected_value = _value(child)
        module_id = module_id or node.attrib.get("Id") or node.attrib.get("id") or ""
        if not module_id:
            continue
        if selected_value in {None, ""}:
            selected_value = node.attrib.get("IsSelected", node.attrib.get("selected"))
        enabled = parse_optional_bool(selected_value)
        result[module_id.casefold()] = {
            # Unknown states are included conservatively by the scanner, but are never
            # presented as a confirmed enabled/disabled choice.
            "enabled": True if enabled is None else enabled,
            "enabled_known": enabled is not None,
            "index": index,
            "name": module_id,
            "is_official": False,
            "source": str(path),
        }
        index += 1
    if not result:
        raise ValueError("В LauncherData.xml не удалось извлечь записи модулей.")
    return result


def parse_load_order_json(path: Path, *, strict: bool = False) -> dict[str, dict[str, Any]]:
    """Read Vortex/BUTR or launcher profile JSON without guessing entry state.

    Supported shapes:
      * BUTR persistence array: id/name/isSelected/isDisabled/index;
      * Vortex exported document: version/loadOrderId/entries, with each entry
        carrying ``enabled`` and ``data.moduleInfoExtended``.

    ``isDisabled`` means that the selection control is locked, not that the mod
    is disabled. ``isSelected`` or the export's explicit ``enabled`` field is
    the source of enabled state.
    """
    try:
        if path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError("JSON-файл порядка загрузки слишком большой для безопасного чтения.")
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        if strict:
            raise ValueError(f"Не удалось прочитать JSON порядка загрузки {path}: {exc}") from exc
        return {}

    entries = data if isinstance(data, list) else None
    is_export_document = False
    if isinstance(data, dict):
        entries = data.get("entries") or data.get("modules") or data.get("modDatas")
        is_export_document = (
            isinstance(data.get("entries"), list)
            and "version" in data
            and "loadOrderId" in data
        )
    if not isinstance(entries, list):
        if strict:
            raise ValueError("В JSON не найден список entries/modules/modDatas.")
        return {}

    # A profile is complete only when its document shape and every entry's
    # explicit selection state prove that it represents a full load-order list.
    # A Vortex provider marker alone is not sufficient: partial exports can
    # contain Vortex-owned entries without describing the whole active profile.
    def _entry_has_id(entry: Any) -> bool:
        if not isinstance(entry, dict):
            return False
        subdata = entry.get("data") if isinstance(entry.get("data"), dict) else {}
        info = subdata.get("moduleInfoExtended") if isinstance(subdata.get("moduleInfoExtended"), dict) else {}
        return bool(str(entry.get("id") or entry.get("moduleId") or entry.get("Id") or info.get("id") or "").strip())

    is_butr_array = bool(entries) and all(
        _entry_has_id(entry)
        and parse_optional_bool(entry.get("isSelected")) is not None
        and parse_optional_bool(entry.get("isDisabled")) is not None
        for entry in entries
    )
    is_complete_export = bool(is_export_document and entries) and all(
        _entry_has_id(entry) and parse_optional_bool(entry.get("enabled")) is not None
        for entry in entries
    )
    profile_is_complete = bool(is_complete_export or is_butr_array)

    result: dict[str, dict[str, Any]] = {}
    for position, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        subdata = entry.get("data") if isinstance(entry.get("data"), dict) else {}
        info = subdata.get("moduleInfoExtended") if isinstance(subdata.get("moduleInfoExtended"), dict) else {}
        module_id = str(entry.get("id") or entry.get("moduleId") or entry.get("Id") or info.get("id") or "").strip()
        if not module_id:
            continue
        try:
            index = int(entry.get("index", entry.get("loadOrder", position)))
        except (TypeError, ValueError):
            index = position

        enabled_value: Any = None
        for key in ("enabled", "isSelected", "selected"):
            if key in entry:
                enabled_value = entry[key]
                break
        enabled = parse_optional_bool(enabled_value)
        official_value = parse_optional_bool(info.get("isOfficial", entry.get("isOfficial", False)))
        is_vortex_entry = (
            ("isSelected" in entry and "isDisabled" in entry)
            or str(info.get("moduleProviderType", "")).casefold() == "vortex"
            or is_export_document
        )
        result[module_id.casefold()] = {
            "enabled": True if enabled is None else enabled,
            "enabled_known": enabled is not None,
            "index": index,
            "name": str(entry.get("name") or info.get("name") or module_id),
            "is_official": bool(official_value),
            "is_locked": bool(parse_optional_bool(entry.get("isDisabled")) or False),
            "source_type": "Vortex (BUTR)" if is_vortex_entry else "JSON profile",
            "profile_complete": profile_is_complete,
            "is_singleplayer_module": parse_optional_bool(info.get("isSingleplayerModule")),
            "is_multiplayer_module": parse_optional_bool(info.get("isMultiplayerModule")),
            "is_server_module": parse_optional_bool(info.get("isServerModule")),
            "source": str(path),
        }
    if strict and not result:
        raise ValueError("В JSON не удалось извлечь записи модулей с Module ID.")
    return result


def load_order_file_profile_metadata(path: Path) -> dict[str, Any]:
    """Describe a selected Vortex/BEM JSON without inventing identity fields."""
    result: dict[str, Any] = {
        "profile_id": None,
        "profile_name": None,
        "game_mode": None,
        "selection_method": "manual_json",
        "load_order_path": str(path),
        "load_order_created_at": None,
        "snapshot_path": None,
        "snapshot_created_at": None,
        "snapshot_age_seconds": None,
        "profile_complete": False,
        "active_profile_verified": False,
    }
    try:
        stat = path.stat()
        result["load_order_created_at"] = datetime.fromtimestamp(
            stat.st_mtime, timezone.utc
        ).isoformat()
        if stat.st_size > 16 * 1024 * 1024:
            raise ValueError("JSON-файл порядка загрузки слишком большой для безопасного чтения.")
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        result["metadata_note"] = f"Не удалось прочитать метаданные профиля из JSON: {exc}"
        data = None

    profile_data: dict[str, Any] = {}
    entries: Any = None
    if isinstance(data, dict):
        raw_profile = data.get("profile") or data.get("profileInfo") or data.get("profile_info")
        if isinstance(raw_profile, dict):
            profile_data = raw_profile
        result["profile_id"] = (
            profile_data.get("id") or profile_data.get("profileId")
            or data.get("profileId") or data.get("profile_id")
        )
        result["profile_name"] = (
            profile_data.get("name") or profile_data.get("profileName")
            or data.get("profileName") or data.get("profile_name")
        )
        raw_mode = (
            profile_data.get("gameMode") or profile_data.get("game_mode")
            or data.get("gameMode") or data.get("game_mode")
        )
        if isinstance(raw_mode, str):
            normalized_mode = raw_mode.strip().casefold()
            if normalized_mode in {"singleplayer", "single-player", "sp"}:
                result["game_mode"] = "singleplayer"
            elif normalized_mode in {"multiplayer", "multi-player", "mp"}:
                result["game_mode"] = "multiplayer"
        entries = data.get("entries") or data.get("modules") or data.get("modDatas")
        result["profile_complete"] = bool(
            isinstance(entries, list) and "version" in data and "loadOrderId" in data
        )
    elif isinstance(data, list):
        entries = data
        result["profile_complete"] = bool(data) and all(
            isinstance(entry, dict) and "isSelected" in entry and "isDisabled" in entry
            for entry in data
        )

    # Infer mode from module capabilities only when the entry set is decisive.
    # Neutral shared modules such as Native are intentionally ignored.
    if isinstance(entries, list) and not result["game_mode"]:
        singleplayer_modules = 0
        multiplayer_modules = 0
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            subdata = entry.get("data") if isinstance(entry.get("data"), dict) else {}
            info = subdata.get("moduleInfoExtended") if isinstance(subdata.get("moduleInfoExtended"), dict) else {}
            if info.get("isSingleplayerModule") is True:
                singleplayer_modules += 1
            if info.get("isMultiplayerModule") is True or info.get("isServerModule") is True:
                multiplayer_modules += 1
        if singleplayer_modules and not multiplayer_modules:
            result["game_mode"] = "singleplayer"
            result["game_mode_inferred_from_modules"] = True
        elif multiplayer_modules and not singleplayer_modules:
            result["game_mode"] = "multiplayer"
            result["game_mode_inferred_from_modules"] = True

    filename = path.name
    if filename.casefold().endswith("_loadorder_multiplayer.json"):
        filename_profile_id = filename[:-len("_loadorder_multiplayer.json")]
        filename_mode = "multiplayer"
    elif filename.casefold().endswith("_loadorder.json"):
        filename_profile_id = filename[:-len("_loadorder.json")]
        filename_mode = "singleplayer"
    else:
        filename_profile_id = None
        filename_mode = None
    if not result["profile_id"]:
        result["profile_id"] = filename_profile_id
    if filename_mode:
        if result["game_mode"] and result["game_mode"] != filename_mode:
            result["metadata_note"] = (
                "Поле режима в JSON противоречит суффиксу имени файла; использован режим из имени файла."
            )
        result["game_mode"] = filename_mode
    if result["profile_id"] is not None:
        result["profile_id"] = str(result["profile_id"])
    if result["profile_name"] is not None:
        result["profile_name"] = str(result["profile_name"])
    if not result["profile_name"]:
        result["profile_name"] = "Не содержится в выбранном JSON"
    if not result["profile_id"]:
        result["profile_id"] = "Не удалось определить из имени файла/JSON"
    if not result["game_mode"]:
        result["game_mode"] = "unknown"
    return result

def _read_vortex_active_profile(path: Path) -> tuple[str, str, str | None, str] | None:
    """Read active Bannerlord profile id/name/mode from a Vortex full-state backup."""
    try:
        if path.stat().st_size > 128 * 1024 * 1024:
            raise ValueError("Снимок состояния Vortex превышает безопасный лимит 128 МБ.")
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise ValueError(f"Не удалось прочитать снимок состояния Vortex {path}: {exc}") from exc

    state = data.get("state") if isinstance(data, dict) and isinstance(data.get("state"), dict) else data
    if not isinstance(state, dict):
        raise ValueError("В снимке Vortex нет объекта состояния.")
    settings = state.get("settings") if isinstance(state.get("settings"), dict) else {}
    persistent = state.get("persistent") if isinstance(state.get("persistent"), dict) else {}
    profiles_settings = settings.get("profiles") if isinstance(settings.get("profiles"), dict) else {}
    active_profile_id = str(profiles_settings.get("activeProfileId") or "").strip()
    profiles = persistent.get("profiles") if isinstance(persistent.get("profiles"), dict) else {}
    profile = profiles.get(active_profile_id) if active_profile_id else None
    if not isinstance(profile, dict):
        raise ValueError("Активный профиль не найден в persistent.profiles снимка Vortex.")
    game_id = str(profile.get("gameId") or "").strip().casefold()
    if game_id != "mountandblade2bannerlord":
        raise ValueError(f"В момент снимка Vortex активен профиль другой игры: {game_id or 'неизвестно'}.")
    profile_name = str(profile.get("name") or active_profile_id)
    features = profile.get("features") if isinstance(profile.get("features"), dict) else {}
    # BUTR's Bannerlord extension defines Singleplayer as the default profile
    # type until the bannerlord_multiplayer feature is explicitly set to true.
    # Mirror that selector rather than treating a missing feature as ambiguous.
    game_mode = (
        "multiplayer"
        if features.get("bannerlord_multiplayer") is True
        else "singleplayer"
    )
    return active_profile_id, profile_name, game_mode, game_id


def _vortex_profile_order_directories(game_root: Path) -> list[Path]:
    """Return known locations for BUTR/Vortex profile order JSON files.

    The Vortex extension stores its profile files in Vortex's Bannerlord
    staging directory (normally under %APPDATA%). Some extension versions or
    local setups also use the game's install directory, so retain that as a
    compatibility location.
    """
    directories: list[Path] = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        directories.append(
            Path(appdata) / "Vortex" / "mountandblade2bannerlord" / "mods"
        )
    directories.append(game_root)
    unique: list[Path] = []
    seen: set[str] = set()
    for directory in directories:
        key = _normalized_path(directory)
        if key not in seen:
            seen.add(key)
            unique.append(directory)
    return unique


def _vortex_profile_file_parts(path: Path) -> tuple[str, str] | None:
    """Extract a safe profile id and game mode from a BUTR load-order filename."""
    match = re.fullmatch(
        r"(?P<profile_id>[A-Za-z0-9._-]{1,128})_loadorder(?P<multiplayer>_multiplayer)?\.json",
        path.name,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    profile_id = match.group("profile_id")
    mode = "multiplayer" if match.group("multiplayer") else "singleplayer"
    return profile_id, mode


def _vortex_profile_order_files(
    game_root: Path,
    diagnostics: list[dict[str, Any]] | None = None,
) -> list[Path]:
    """List profile order files in Vortex staging and legacy game-root locations."""
    rows = diagnostics if diagnostics is not None else []
    found: dict[str, Path] = {}
    for directory in _vortex_profile_order_directories(game_root):
        try:
            exists = directory.is_dir()
        except OSError as exc:
            rows.append({
                "path": str(directory), "status": "unreadable",
                "reason": f"Не удалось проверить каталог Vortex load order: {exc}",
            })
            continue
        if not exists:
            rows.append({
                "path": str(directory), "status": "directory_not_found",
                "reason": "Каталог хранения профильных JSON Vortex не существует.",
            })
            continue
        rows.append({
            "path": str(directory), "status": "directory_found",
            "reason": "Каталог хранения профильных JSON Vortex найден; проверяются файлы профилей.",
        })
        try:
            for path in directory.iterdir():
                if not path.is_file() or _vortex_profile_file_parts(path) is None:
                    continue
                # Reject symbolic links that escape the directory being scanned.
                if path.resolve(strict=False).parent != directory.resolve(strict=False):
                    rows.append({
                        "path": str(path), "status": "rejected",
                        "reason": "Файл порядка является ссылкой за пределы каталога профилей Vortex.",
                    })
                    continue
                found.setdefault(_normalized_path(path), path)
        except OSError as exc:
            rows.append({
                "path": str(directory), "status": "unreadable",
                "reason": f"Не удалось перечислить файлы профилей Vortex: {exc}",
            })
    return list(found.values())


def _find_profile_order_file(
    game_root: Path,
    profile_id: str,
    game_mode: str,
    diagnostics: list[dict[str, Any]],
) -> Path | None:
    """Find the exact profile-id/mode file, preferring Vortex's own mods folder."""
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", profile_id):
        raise ValueError("ID активного профиля Vortex содержит недопустимые символы; путь не сформирован.")
    suffix = "_loadorder_multiplayer.json" if game_mode == "multiplayer" else "_loadorder.json"
    filename = f"{profile_id}{suffix}"
    matches: list[tuple[int, float, Path]] = []
    for preference, directory in enumerate(_vortex_profile_order_directories(game_root)):
        candidate = directory / filename
        try:
            if not candidate.is_file():
                diagnostics.append({
                    "path": str(candidate), "status": "not_found",
                    "reason": "Файл порядка активного профиля не найден в этом каталоге.",
                })
                continue
            if candidate.resolve(strict=False).parent != directory.resolve(strict=False):
                diagnostics.append({
                    "path": str(candidate), "status": "rejected",
                    "reason": "Файл порядка выходит за пределы каталога профилей Vortex.",
                })
                continue
            stat = candidate.stat()
            # The AppData location is the current Vortex extension's canonical
            # store. Prefer it over a legacy game-root copy even if the latter
            # has a newer mtime; mtime is only a tie-breaker within a location.
            matches.append((-preference, stat.st_mtime, candidate))
            diagnostics.append({
                "path": str(candidate), "status": "candidate_found",
                "file_mtime_utc": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                "reason": "Файл совпадает с ID и режимом активного профиля из свежего состояния Vortex.",
            })
        except OSError as exc:
            diagnostics.append({
                "path": str(candidate), "status": "unreadable",
                "reason": f"Не удалось проверить файл порядка профиля: {exc}",
            })
    if not matches:
        return None
    matches.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return matches[0][2]


def _parse_vortex_profile_candidate(
    order_path: Path,
    *,
    profile_id: str,
    game_mode: str,
    module_ids: set[str],
    diagnostics: list[dict[str, Any]],
    profile_name: str | None = None,
    require_recent: bool = False,
    max_file_age_seconds: int = 30 * 60,
    now: float | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]] | None:
    """Parse and validate one profile file against installed Module IDs."""
    row = next((item for item in reversed(diagnostics) if item.get("path") == str(order_path)), None)
    try:
        stat = order_path.stat()
        if stat.st_size > 16 * 1024 * 1024:
            raise ValueError("JSON-файл профиля превышает безопасный лимит 16 МБ.")
        if not order_path.is_file():
            raise ValueError("Путь профиля не является обычным файлом.")
        parsed = _vortex_profile_file_parts(order_path)
        if parsed is None:
            raise ValueError("Имя файла не соответствует шаблону <profileId>_loadorder[_multiplayer].json.")
        filename_profile_id, filename_mode = parsed
        if filename_profile_id.casefold() != profile_id.casefold():
            raise ValueError("ID профиля в имени файла не совпадает с ожидаемым активным профилем.")
        if filename_mode != game_mode:
            raise ValueError("Режим игры в суффиксе файла не совпадает с режимом активного профиля.")
        order = parse_load_order_json(order_path, strict=True)
        if not all(entry.get("profile_complete") for entry in order.values()):
            raise ValueError("JSON прочитан, но формат не подтверждает полный список Vortex/BUTR.")
        overlap_ids = set(order) & {str(module_id).casefold() for module_id in module_ids}
        overlap_ratio = len(overlap_ids) / max(1, len(order))
        if not overlap_ids or overlap_ratio < 0.5:
            raise ValueError(
                f"Недостаточно совпадений с установленными Module ID: {len(overlap_ids)} из {len(order)}; "
                "возможно, файл относится к другой игре или установке."
            )
        current_time = time.time() if now is None else now
        age_seconds = max(0.0, current_time - stat.st_mtime)
        if require_recent and age_seconds > max_file_age_seconds:
            raise TimeoutError(
                f"Файл профиля не обновлялся недавно: возраст {age_seconds / 60:.1f} мин.; "
                f"для автоматического вывода допустимо не более {max_file_age_seconds / 60:.0f} мин."
            )
    except TimeoutError as exc:
        if row is not None:
            row.update({"status": "stale", "reason": str(exc)})
        return None
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        if row is not None:
            row.update({"status": "rejected", "reason": str(exc)})
        else:
            diagnostics.append({"path": str(order_path), "status": "rejected", "reason": str(exc)})
        return None

    unknown_profile_name = profile_name or "Не удалось определить из состояния Vortex"
    for entry in order.values():
        entry["source_type"] = "Vortex (BUTR)"
        entry["profile_complete"] = True
        entry["game_mode"] = game_mode
        entry["profile_id"] = profile_id
        entry["profile_name"] = unknown_profile_name

    file_mtime = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
    profile_info = {
        "provider": "Vortex / BUTR",
        "profile_id": profile_id,
        "profile_name": unknown_profile_name,
        "game_mode": game_mode,
        "profile_complete": True,
        "load_order_path": str(order_path),
        "load_order_created_at": file_mtime,
        "load_order_entries": len(order),
        "matched_installed_modules": len(overlap_ids),
        "unmatched_profile_module_ids": sorted(set(order) - {str(x).casefold() for x in module_ids}),
        "installed_module_ids_missing_from_profile": sorted({str(x).casefold() for x in module_ids} - set(order)),
        "load_order_file_age_seconds": round(max(0.0, (time.time() if now is None else now) - stat.st_mtime), 1),
    }
    if row is not None:
        row.update({
            "status": "accepted",
            "profile_id": profile_id,
            "profile_name": unknown_profile_name,
            "game_mode": game_mode,
            "entries": len(order),
            "matched_installed_modules": len(overlap_ids),
            "file_mtime_utc": file_mtime,
            "reason": "JSON соответствует ID/режиму профиля и содержит достаточно Module ID из выбранной установки.",
        })
    else:
        diagnostics.append({
            "path": str(order_path), "status": "accepted",
            "profile_id": profile_id, "profile_name": unknown_profile_name,
            "game_mode": game_mode, "entries": len(order),
            "matched_installed_modules": len(overlap_ids), "file_mtime_utc": file_mtime,
            "reason": "JSON соответствует ID/режиму профиля и содержит достаточно Module ID из выбранной установки.",
        })
    return order, profile_info


def _discover_recent_vortex_profile_file(
    game_root: Path,
    module_ids: set[str],
    notes: list[str],
    diagnostics: list[dict[str, Any]],
    profile_info: dict[str, Any] | None,
    *,
    max_file_age_seconds: int = 30 * 60,
) -> tuple[dict[str, dict[str, Any]], str | None]:
    """Best-effort fallback: choose a uniquely freshest recently saved profile file.

    File recency is not proof of Vortex's selected profile. The report therefore
    records this source as inferred rather than state-confirmed.
    """
    files = _vortex_profile_order_files(game_root, diagnostics)
    if profile_info is not None:
        profile_info["vortex_order_files_found"] = bool(files)
    if not files:
        notes.append("В каталогах Vortex и игры не найдены файлы *_loadorder.json профилей.")
        return {}, None

    now = time.time()
    candidates_by_profile_mode: dict[
        tuple[str, str],
        tuple[float, int, str, str, Path, dict[str, dict[str, Any]], dict[str, Any]],
    ] = {}
    directory_preferences = {
        _normalized_path(directory): index
        for index, directory in enumerate(_vortex_profile_order_directories(game_root))
    }
    for path in files:
        parts = _vortex_profile_file_parts(path)
        if parts is None:
            continue
        profile_id, game_mode = parts
        stat_row = next((item for item in reversed(diagnostics) if item.get("path") == str(path)), None)
        try:
            stat = path.stat()
        except OSError as exc:
            if stat_row is not None:
                stat_row.update({"status": "unreadable", "reason": f"Не удалось получить атрибуты файла: {exc}"})
            continue
        age_seconds = max(0.0, now - stat.st_mtime)
        if stat_row is not None:
            stat_row.update({
                "profile_id": profile_id,
                "game_mode": game_mode,
                "file_mtime_utc": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                "age_seconds": round(age_seconds, 1),
            })
        if age_seconds > max_file_age_seconds:
            reason = (
                f"Файл профиля устарел для вывода активного профиля: возраст {age_seconds / 60:.1f} мин.; "
                f"допустимо не более {max_file_age_seconds / 60:.0f} мин."
            )
            if stat_row is not None:
                stat_row.update({"status": "stale", "reason": reason})
            continue
        if stat_row is not None:
            stat_row.update({"status": "candidate", "reason": "Свежий профильный JSON; проверяется его содержимое и совпадение Module ID."})
        parsed = _parse_vortex_profile_candidate(
            path,
            profile_id=profile_id,
            game_mode=game_mode,
            module_ids=module_ids,
            diagnostics=diagnostics,
            require_recent=True,
            max_file_age_seconds=max_file_age_seconds,
            now=now,
        )
        if parsed is None:
            continue
        order, candidate_info = parsed
        preference = directory_preferences.get(_normalized_path(path.parent), len(directory_preferences))
        candidate_key = (profile_id.casefold(), game_mode)
        candidate = (stat.st_mtime, preference, profile_id.casefold(), game_mode, path, order, candidate_info)
        existing = candidates_by_profile_mode.get(candidate_key)
        if existing is None or (preference, -stat.st_mtime) < (existing[1], -existing[0]):
            candidates_by_profile_mode[candidate_key] = candidate

    candidates = list(candidates_by_profile_mode.values())
    if not candidates:
        notes.append(
            "Файлы профилей Vortex найдены, но ни один свежий кандидат не прошёл проверку формата и совпадения Module ID. "
            "Выберите JSON профиля вручную."
        )
        return {}, None

    # If several profile/mode files were edited at effectively the same time,
    # recency cannot safely identify the active one.
    candidates.sort(key=lambda item: item[0], reverse=True)
    if len(candidates) > 1:
        newest_age_delta = candidates[0][0] - candidates[1][0]
        if newest_age_delta < 2.0:
            reason = (
                "Несколько профильных JSON обновлены почти одновременно; по времени изменения нельзя надёжно определить активный профиль. "
                "Выберите JSON вручную или создайте новый снимок состояния Vortex."
            )
            notes.append(reason)
            for _mtime, _preference, _pid, _mode, path, _order, _info in candidates:
                row = next((item for item in reversed(diagnostics) if item.get("path") == str(path)), None)
                if row is not None:
                    row.update({"status": "ambiguous", "reason": reason})
            return {}, None

    _mtime, _preference, _pid, _mode, selected_path, selected_order, selected_info = candidates[0]
    selected_info.update({
        "selection_method": "latest_recent_profile_file",
        "active_profile_verified": False,
        "active_profile_verification": "inferred_from_unique_recent_file_mtime",
        "profile_name": "Не удалось получить из активного состояния Vortex",
    })
    for entry in selected_order.values():
        entry["profile_name"] = selected_info["profile_name"]
    reason = (
        f"Профиль Vortex {selected_info['profile_id']} ({selected_info['game_mode']}) выбран по единственному самому свежему файлу порядка; "
        f"mtime {selected_info['load_order_created_at']}. Активный профиль напрямую из состояния Vortex не подтверждён."
    )
    notes.append(reason)
    selected_row = next((item for item in reversed(diagnostics) if item.get("path") == str(selected_path)), None)
    if selected_row is not None:
        selected_row.update({"status": "accepted_inferred", "reason": reason})
    if profile_info is not None:
        profile_info.update(selected_info)
    # Candidates for other profiles remain visible, but are not treated as selected.
    for _mtime, _preference, _pid, _mode, path, _order, _info in candidates[1:]:
        row = next((item for item in reversed(diagnostics) if item.get("path") == str(path)), None)
        if row is not None and row.get("status") == "accepted":
            row.update({"status": "not_selected", "reason": "Есть более свежий однозначный профильный JSON; этот файл относится к другому профилю/режиму."})
    return selected_order, str(selected_path)


def _discover_vortex_load_order(
    game_root: Path,
    module_ids: set[str],
    notes: list[str],
    *,
    max_snapshot_age_seconds: int = 30 * 60,
    diagnostics: list[dict[str, Any]] | None = None,
    profile_info: dict[str, Any] | None = None,
) -> tuple[dict[str, dict[str, Any]], str | None]:
    """Resolve Vortex/BUTR load order using state snapshots, then recent profile JSON.

    The current state snapshot is authoritative. If none is fresh, a uniquely
    freshest profile JSON updated within 30 minutes may be used as an explicitly
    unverified inference. BEM/Launcher fallbacks are handled by discover_load_order
    only after this function has exhausted Vortex locations.
    """
    diagnostic_rows = diagnostics if diagnostics is not None else []
    appdata = os.environ.get("APPDATA")
    snapshots: list[tuple[float, Path, int]] = []
    now = time.time()
    if not appdata:
        reason = "Переменная APPDATA не задана; снимки состояния Vortex не проверены."
        notes.append(reason)
        diagnostic_rows.append({
            "path": "%APPDATA%\\Vortex\\temp\\state_backups_full",
            "status": "not_checked", "reason": reason,
        })
    else:
        backup_dir = Path(appdata) / "Vortex" / "temp" / "state_backups_full"
        candidate_paths = [backup_dir / name for name in ("manual.json", "startup.json", "hourly.json")]
        for path in candidate_paths:
            try:
                stat = path.stat()
            except FileNotFoundError:
                diagnostic_rows.append({"path": str(path), "status": "not_found", "reason": "Файл снимка не существует."})
                continue
            except OSError as exc:
                diagnostic_rows.append({"path": str(path), "status": "unreadable", "reason": f"Не удалось получить атрибуты файла: {exc}"})
                continue
            if not path.is_file():
                diagnostic_rows.append({"path": str(path), "status": "not_file", "reason": "Путь существует, но не является обычным файлом."})
                continue
            age_seconds = max(0.0, now - stat.st_mtime)
            file_mtime = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
            if age_seconds > max_snapshot_age_seconds:
                reason = (
                    f"Снимок устарел: возраст {age_seconds / 60:.1f} мин., "
                    f"допустимо не более {max_snapshot_age_seconds / 60:.0f} мин."
                )
                diagnostic_rows.append({
                    "path": str(path), "status": "stale", "exists": True,
                    "file_mtime_utc": file_mtime, "age_seconds": round(age_seconds, 1),
                    "reason": reason,
                })
                notes.append(f"{path}: {reason}")
                continue
            diagnostic_rows.append({
                "path": str(path), "status": "candidate", "exists": True,
                "file_mtime_utc": file_mtime, "age_seconds": round(age_seconds, 1),
                "reason": "Свежий снимок состояния Vortex; проверяется активный профиль.",
            })
            snapshots.append((stat.st_mtime, path, int(stat.st_size)))

    known_module_ids = {str(module_id).casefold() for module_id in module_ids}
    snapshots.sort(key=lambda item: item[0], reverse=True)
    for _mtime, snapshot, snapshot_size in snapshots:
        row = next((item for item in reversed(diagnostic_rows) if item.get("path") == str(snapshot)), None)
        try:
            if snapshot_size > 128 * 1024 * 1024:
                raise ValueError("Снимок состояния Vortex превышает безопасный лимит 128 МБ.")
            active_profile = _read_vortex_active_profile(snapshot)
        except ValueError as exc:
            reason = str(exc)
            notes.append(reason)
            if row is not None:
                row.update({"status": "rejected", "reason": reason})
            continue
        if active_profile is None:
            reason = "В снимке не удалось однозначно определить активный профиль."
            notes.append(reason)
            if row is not None:
                row.update({"status": "rejected", "reason": reason})
            continue

        profile_id, profile_name, game_mode, game_id = active_profile
        try:
            order_path = _find_profile_order_file(game_root, profile_id, game_mode, diagnostic_rows)
        except ValueError as exc:
            reason = str(exc)
            notes.append(reason)
            if row is not None:
                row.update({"status": "rejected", "reason": reason})
            continue
        if order_path is None:
            reason = (
                f"Для активного профиля Vortex {profile_id} ({game_mode}) не найден JSON порядка "
                "в %APPDATA%\\Vortex\\mountandblade2bannerlord\\mods или корне игры."
            )
            notes.append(reason)
            if row is not None:
                row.update({"status": "rejected", "reason": reason})
            continue

        parsed = _parse_vortex_profile_candidate(
            order_path,
            profile_id=profile_id,
            game_mode=game_mode,
            module_ids=known_module_ids,
            diagnostics=diagnostic_rows,
            profile_name=profile_name,
        )
        if parsed is None:
            reason = f"Файл порядка для активного профиля {profile_id} не прошёл проверку содержания."
            notes.append(reason)
            if row is not None:
                row.update({"status": "rejected", "reason": reason})
            continue
        order, info = parsed
        try:
            snapshot_stat = snapshot.stat()
        except OSError as exc:
            reason = f"Не удалось повторно проверить снимок Vortex: {exc}"
            notes.append(reason)
            if row is not None:
                row.update({"status": "rejected", "reason": reason})
            continue
        age_seconds = max(0.0, time.time() - snapshot_stat.st_mtime)
        if age_seconds > max_snapshot_age_seconds:
            reason = (
                f"Снимок устарел во время проверки: возраст {age_seconds / 60:.1f} мин.; "
                f"допустимо не более {max_snapshot_age_seconds / 60:.0f} мин."
            )
            notes.append(reason)
            if row is not None:
                row.update({"status": "stale", "age_seconds": round(age_seconds, 1), "reason": reason})
            continue
        snapshot_mtime = datetime.fromtimestamp(snapshot_stat.st_mtime, timezone.utc).isoformat()
        info.update({
            "selection_method": "automatic_fresh_snapshot",
            "active_profile_verified": True,
            "active_profile_verification": "fresh_vortex_state_snapshot",
            "game_id": game_id,
            "snapshot_path": str(snapshot),
            "snapshot_created_at": snapshot_mtime,
            "snapshot_timestamp_kind": "filesystem_mtime_utc",
            "snapshot_age_seconds": round(age_seconds, 1),
            "snapshot_max_age_seconds": max_snapshot_age_seconds,
        })
        for entry in order.values():
            entry["profile_name"] = profile_name
        reason = (
            f"Активный профиль Vortex '{profile_name}' ({game_mode}); ID {profile_id}; "
            f"свежий снимок mtime (UTC) {snapshot_mtime}; файл порядка найден: {order_path}."
        )
        notes.append(reason)
        if row is not None:
            row.update({"status": "accepted", "reason": reason, "profile_id": profile_id, "profile_name": profile_name, "game_mode": game_mode, "load_order_path": str(order_path)})
        if profile_info is not None:
            profile_info.update(info)
        return order, str(order_path)

    # A fresh state snapshot is not always available. Vortex updates the
    # profile-specific JSON when the load order is changed. A single uniquely
    # freshest file modified within the same 30-minute window is a useful
    # fallback, but is explicitly marked as inferred rather than confirmed.
    return _discover_recent_vortex_profile_file(
        game_root, known_module_ids, notes, diagnostic_rows, profile_info,
        max_file_age_seconds=max_snapshot_age_seconds,
    )

def _documents_candidates() -> list[Path]:
    roots: list[Path] = []
    home = Path.home()
    roots.extend([home / "Documents", home / "OneDrive" / "Documents"])
    for env_key in ("USERPROFILE", "OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        value = os.environ.get(env_key)
        if value:
            base = Path(value)
            roots.append(base / "Documents")
            if env_key.startswith("OneDrive"):
                roots.append(base)
    if os.name == "nt":
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(32768)
            if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0:
                roots.append(Path(buf.value))
        except Exception:
            pass
    unique: list[Path] = []
    seen: set[str] = set()
    for path in roots:
        key = str(path).casefold()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def _normalized_path(path: Path | str) -> str:
    try:
        return os.path.normcase(str(Path(path).expanduser().resolve(strict=False))).casefold()
    except (OSError, RuntimeError, ValueError):
        return os.path.normcase(os.path.abspath(os.path.normpath(str(path)))).casefold()


def _bem_launcher_data_path(
    game_root: Path, diagnostics: list[dict[str, Any]] | None = None
) -> tuple[Path | None, bool, list[str]]:
    """Resolve BEM's LauncherData.xml through its instance registry with full diagnostics."""
    local_app = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    bem_root = local_app / "Bannerlord Environment Manager"
    settings_path = bem_root / "instances.json"
    notes: list[str] = []
    diagnostic_rows = diagnostics if diagnostics is not None else []
    registry_row = {
        "path": str(settings_path), "status": "not_found",
        "reason": "Реестр экземпляров BEM не найден; проверка BEM пропущена.",
    }
    diagnostic_rows.append(registry_row)
    if not settings_path.is_file():
        return None, False, notes

    registry_row.update({
        "status": "candidate_found",
        "reason": "Найден реестр экземпляров BEM; проверяется конфигурация и сопоставление игры.",
    })
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8-sig"))
        games_root_value = settings.get("GamesRoot", "") if isinstance(settings, dict) else ""
        resting_id = str(settings.get("RestingInstanceId", "") or "") if isinstance(settings, dict) else ""
        if not games_root_value:
            reason = "GamesRoot не задан в настройках BEM."
            registry_row.update({"status": "rejected", "reason": reason})
            notes.append(f"{settings_path}: {reason}")
            return None, True, notes
        games_root = Path(str(games_root_value)).expanduser()
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        reason = f"Не удалось прочитать реестр экземпляров BEM: {exc}"
        registry_row.update({"status": "rejected", "reason": reason})
        notes.append(f"{settings_path}: {reason}")
        return None, True, notes

    games_root_row = {
        "path": str(games_root), "status": "candidate_found",
        "reason": "GamesRoot прочитан из реестра BEM; проверяется доступность каталога.",
    }
    diagnostic_rows.append(games_root_row)
    if not games_root.is_absolute() or not games_root.is_dir():
        reason = "GamesRoot недоступен или не является абсолютным путём."
        games_root_row.update({"status": "rejected", "reason": reason})
        registry_row.update({"status": "rejected", "reason": reason})
        notes.append(f"{settings_path}: {reason}")
        return None, True, notes

    wanted_game = _normalized_path(game_root)
    matches: list[tuple[Path, str]] = []
    try:
        folders = list(games_root.iterdir())
    except OSError as exc:
        reason = f"Не удалось просмотреть экземпляры BEM: {exc}"
        games_root_row.update({"status": "unreadable", "reason": reason})
        registry_row.update({"status": "rejected", "reason": reason})
        notes.append(f"{games_root}: {reason}")
        return None, True, notes

    for folder in folders:
        if not folder.is_dir():
            continue
        metadata_path = folder / "instance.json"
        if not metadata_path.is_file():
            diagnostic_rows.append({
                "path": str(metadata_path), "status": "not_found",
                "reason": "Папка не содержит instance.json; не является пригодным экземпляром BEM.",
            })
            continue
        try:
            record = json.loads(metadata_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            diagnostic_rows.append({
                "path": str(metadata_path), "status": "rejected",
                "reason": f"Не удалось прочитать instance.json: {exc}",
            })
            continue
        if not isinstance(record, dict):
            diagnostic_rows.append({
                "path": str(metadata_path), "status": "rejected",
                "reason": "instance.json не содержит JSON-объект.",
            })
            continue
        recorded_game = record.get("GameFolder")
        instance_id = str(record.get("Id", "") or "")
        if recorded_game and instance_id and _normalized_path(str(recorded_game)) == wanted_game:
            matches.append((folder, instance_id))
            diagnostic_rows.append({
                "path": str(metadata_path), "status": "matched",
                "reason": f"Экземпляр BEM {instance_id} указывает на выбранную папку игры.",
            })
        else:
            diagnostic_rows.append({
                "path": str(metadata_path), "status": "not_matching_game",
                "reason": "Экземпляр BEM относится к другой папке игры или не содержит полного GameFolder/Id.",
            })

    if len(matches) > 1:
        reason = "Несколько экземпляров BEM указывают на выбранную папку игры; источник неоднозначен."
        games_root_row.update({"status": "ambiguous", "reason": reason})
        notes.append(f"{games_root}: {reason}")
        return None, True, notes
    if not matches:
        reason = (
            f"Для выбранной папки игры не найден подходящий экземпляр BEM: {game_root}; папка игры не сопоставлена с реестром. "
            "Профиль из общих Documents не используется, чтобы не принять порядок другой установки за текущий."
        )
        games_root_row.update({"status": "not_matching_game", "reason": reason})
        notes.append(reason)
        return None, True, notes

    instance_folder, instance_id = matches[0]
    if instance_id == resting_id:
        docs_candidates = [
            docs / "Mount and Blade II Bannerlord" / "Configs" / "LauncherData.xml"
            for docs in _documents_candidates()
        ]
        for candidate in docs_candidates:
            try:
                exists = candidate.is_file()
                status = "candidate_found" if exists else "not_found"
                reason = (
                    "LauncherData.xml найден для resting-экземпляра BEM."
                    if exists else "Файл LauncherData.xml не найден по этому Documents-пути."
                )
            except OSError as exc:
                status, reason = "unreadable", f"Не удалось проверить Documents-путь: {exc}"
            diagnostic_rows.append({"path": str(candidate), "status": status, "reason": reason})
        existing = _unique_existing_files(docs_candidates)
        if len(existing) == 1:
            registry_row.update({"status": "matched", "reason": f"Выбран resting-экземпляр BEM {instance_id}; профиль ищется в Documents."})
            return existing[0], True, [f"Выбран экземпляр BEM, размещённый в стандартных Documents: {instance_folder.name}"]
        reason = (
            "Экземпляр BEM использует стандартные Documents, но найдено несколько LauncherData.xml."
            if len(existing) > 1 else
            f"Для экземпляра BEM {instance_folder.name} не найден LauncherData.xml в стандартных Documents."
        )
        registry_row.update({"status": "rejected", "reason": reason})
        notes.append(reason)
        return None, True, notes

    launcher_path = (
        instance_folder / "UserData" / "Documents" / "Mount and Blade II Bannerlord"
        / "Configs" / "LauncherData.xml"
    )
    try:
        exists = launcher_path.is_file()
        status = "candidate_found" if exists else "not_found"
        reason = (
            f"Файл профиля найден в выбранном экземпляре BEM {instance_folder.name}."
            if exists else f"Для выбранного экземпляра BEM {instance_folder.name} LauncherData.xml не найден."
        )
    except OSError as exc:
        exists, status, reason = False, "unreadable", f"Не удалось проверить путь LauncherData.xml: {exc}"
    diagnostic_rows.append({"path": str(launcher_path), "status": status, "reason": reason})
    if exists:
        registry_row.update({"status": "matched", "reason": f"Выбран экземпляр BEM {instance_id}: LauncherData.xml найден."})
        return launcher_path, True, [f"Выбран соответствующий экземпляр BEM: {instance_folder.name}"]
    registry_row.update({"status": "rejected", "reason": reason})
    notes.append(reason)
    return None, True, notes

def _unique_existing_files(paths: list[Path]) -> list[Path]:
    found: dict[str, Path] = {}
    for path in paths:
        if not path.is_file():
            continue
        found.setdefault(_normalized_path(path), path)
    return list(found.values())


def discover_load_order(
    game_root: Path,
    module_ids: set[str],
    progress_callback=None,
    *,
    profile_info: dict[str, Any] | None = None,
    diagnostics: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, dict[str, Any]], str | None, list[str]]:
    r"""Find the active Vortex/BEM/Launcher order for the selected game.

    Vortex is resolved from a recent full-state backup and its per-profile
    load-order JSON under %APPDATA%\Vortex\mountandblade2bannerlord\mods
    (with the game root retained as a compatibility location). A uniquely newest
    recent profile file can be used as an explicitly inferred fallback. BEM and
    the vanilla Launcher are considered only when no Vortex profile files exist.
    """
    checked: list[str] = []
    if progress_callback:
        progress_callback({"type": "progress", "stage": "profile", "label": "Определение профиля Vortex/BEM/Launcher", "current": 1, "total": 3, "percent": 17, "detail": "Проверка активного профиля Vortex"})

    diagnostic_rows = diagnostics if diagnostics is not None else []
    vortex_order, vortex_source = _discover_vortex_load_order(
        game_root, module_ids, checked, diagnostics=diagnostic_rows, profile_info=profile_info
    )
    if vortex_order:
        verified = bool((profile_info or {}).get("active_profile_verified"))
        label = "Активный профиль Vortex подтверждён" if verified else "Файл профиля Vortex определён по времени изменения"
        if progress_callback:
            progress_callback({"type": "progress", "stage": "profile", "label": label, "current": 3, "total": 3, "percent": 19, "detail": Path(vortex_source or "").name})
        return vortex_order, vortex_source, checked

    # If Vortex profile files exist but their active identity cannot be
    # established safely, do not fall through to stale BEM/Launcher state.
    # This avoids letting remnants of an old manager override the current Vortex
    # installation or choosing a different game's Documents profile.
    remaining_vortex_files = _vortex_profile_order_files(game_root)
    if remaining_vortex_files:
        reason = (
            f"Найдены профильные JSON Vortex ({len(remaining_vortex_files)} файл(ов)), "
            "но ни один не удалось безопасно сопоставить с активным профилем. "
            "Остаточные настройки BEM/Launcher пропущены; выберите файл профиля вручную."
        )
        checked.append(reason)
        for vortex_file in remaining_vortex_files:
            if not any(item.get("path") == str(vortex_file) for item in diagnostic_rows):
                diagnostic_rows.append({
                    "path": str(vortex_file), "status": "not_selected",
                    "reason": "Профильный JSON найден, но автоматический выбор не прошёл проверку активного профиля/свежести/состава модулей.",
                })
        return {}, None, checked

    bem_path, bem_registry_present, bem_notes = _bem_launcher_data_path(game_root, diagnostic_rows)
    checked.extend(bem_notes)
    selected_path: Path | None = None
    if bem_registry_present:
        # Once a BEM registry exists, never fall back to a possibly unrelated Documents file.
        selected_path = bem_path
    else:
        vortex_files = _vortex_profile_order_files(game_root)
        for vortex_file in vortex_files:
            diagnostic_rows.append({
                "path": str(vortex_file), "status": "not_selected",
                "reason": "Файл профиля найден, но активный ID профиля не подтверждён снимком Vortex; автоматический выбор запрещён."
            })
        if vortex_files:
            checked.append(
                f"Найдены {len(vortex_files)} сохранённых файла(ов) порядка Vortex/BUTR, "
                "но активный профиль Vortex не подтверждён. BCI не выбирает файл по имени/времени "
                "и не подменяет его порядком обычного Launcher; выберите JSON активного профиля вручную."
            )
            return {}, None, checked

        documents_candidates = [
            docs / "Mount and Blade II Bannerlord" / "Configs" / "LauncherData.xml"
            for docs in _documents_candidates()
        ]
        for candidate in documents_candidates:
            exists = candidate.is_file()
            diagnostic_rows.append({
                "path": str(candidate), "status": "candidate_found" if exists else "not_found",
                "reason": "Кандидат LauncherData.xml найден." if exists else "Файл LauncherData.xml не найден по этому пути."
            })
        documents_files = _unique_existing_files(documents_candidates)
        if len(documents_files) == 1:
            selected_path = documents_files[0]
        elif len(documents_files) > 1:
            checked.append("Найдено несколько различных LauncherData.xml в Documents; автоматический выбор отменён")
        else:
            # Non-standard/legacy locations are only fallbacks when the canonical user profile is absent.
            legacy_candidates = [game_root / "Configs" / "LauncherData.xml", game_root / "LauncherData.xml"]
            for candidate in legacy_candidates:
                exists = candidate.is_file()
                diagnostic_rows.append({
                    "path": str(candidate), "status": "candidate_found" if exists else "not_found",
                    "reason": "Legacy LauncherData.xml найден." if exists else "Legacy LauncherData.xml не найден."
                })
            legacy_files = _unique_existing_files(legacy_candidates)
            if len(legacy_files) == 1:
                selected_path = legacy_files[0]
            elif len(legacy_files) > 1:
                checked.append("Найдено несколько LauncherData.xml рядом с игрой; автоматический выбор отменён")

    if progress_callback:
        progress_callback({"type": "progress", "stage": "profile", "label": "Чтение профиля загрузки", "current": 3, "total": 3, "percent": 19, "detail": selected_path.name if selected_path else "Надёжный профиль не найден"})

    if selected_path is None:
        if not checked:
            checked.append("Не найдено соответствие для LauncherData.xml выбранной игры")
        return {}, None, checked

    selected_diagnostic = next(
        (item for item in reversed(diagnostic_rows) if item.get("path") == str(selected_path)), None
    )
    try:
        order = parse_launcher_data(selected_path)
    except Exception as exc:
        reason = f"LauncherData.xml не удалось разобрать: {exc}"
        checked.append(f"{selected_path}: {reason}")
        if selected_diagnostic is not None:
            selected_diagnostic.update({"status": "rejected", "reason": reason})
        return {}, None, checked

    source_type = "BEM" if bem_registry_present else "Launcher"
    for entry in order.values():
        entry["source_type"] = source_type
    try:
        order_stat = selected_path.stat()
        order_created_at = datetime.fromtimestamp(order_stat.st_mtime, timezone.utc).isoformat()
    except OSError:
        order_created_at = None
    if profile_info is not None:
        profile_info.update({
            "provider": source_type, "selection_method": "automatic_launcher_discovery",
            "profile_id": None, "profile_name": "Не указано в LauncherData.xml",
            "game_mode": "singleplayer" if source_type == "Launcher" else "unknown",
            "load_order_path": str(selected_path), "load_order_created_at": order_created_at,
            "snapshot_path": None, "snapshot_created_at": None, "snapshot_age_seconds": None,
        })

    overlap = len(set(order) & {module_id.casefold() for module_id in module_ids})
    if overlap == 0:
        reason = "Нет совпадающих Module ID с установленными модулями; профиль не принят."
        checked.append(f"{selected_path}: {reason}")
        if selected_diagnostic is not None:
            selected_diagnostic.update({"status": "rejected", "reason": reason})
        return {}, None, checked

    checked.append(f"{selected_path}: совпадений Module ID — {overlap}")
    if selected_diagnostic is not None:
        selected_diagnostic.update({
            "status": "accepted",
            "reason": f"Профиль прочитан; совпадений Module ID — {overlap}.",
            "file_mtime_utc": order_created_at,
            "entries": len(order), "matched_installed_modules": overlap,
        })
    else:
        diagnostic_rows.append({
            "path": str(selected_path), "status": "accepted",
            "reason": f"Профиль прочитан; совпадений Module ID — {overlap}.",
            "file_mtime_utc": order_created_at, "entries": len(order), "matched_installed_modules": overlap,
        })
    return order, str(selected_path), checked
