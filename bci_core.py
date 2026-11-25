from __future__ import annotations

import fnmatch
import hashlib
import html
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from bci_environment import (
    discover_load_order, load_order_file_profile_metadata, parse_load_order_json,
    parse_optional_bool, resolve_game_paths,
)
from bci_store import BCIStore

APP_NAME = "Bannerlord Compatibility Intelligence"
APP_VERSION = "0.5.2-alpha"
OFFICIAL_FOLDER_IDS = {
    "native", "sandboxcore", "sandbox", "storymode", "custombattle",
    "birthanddeath", "navaldlc", "multiplayer", "dedicatedcustomserverteam",
}
NETWORK_ONLY_MODULE_IDS = {"multiplayer", "dedicatedcustomserverteam"}
XML_EXTENSIONS = {".xml"}
XSLT_EXTENSIONS = {".xsl", ".xslt"}
RESOURCE_DIR_HINTS = {
    "assets", "asset", "sceneobj", "scenes", "sceneedits", "gui", "textures",
    "meshes", "materials", "sounds", "music", "spriteparts", "prefabs"
}


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def norm(value: str | None) -> str:
    return (value or "").strip().casefold()


def child_value(parent: ET.Element | None, name: str, default: str = "") -> str:
    if parent is None:
        return default
    for el in list(parent):
        if local_name(el.tag).casefold() == name.casefold():
            return el.attrib.get("value") or el.attrib.get("id") or (el.text or "").strip() or default
    return default


def iter_children(parent: ET.Element | None, name: str) -> list[ET.Element]:
    if parent is None:
        return []
    return [x for x in list(parent) if local_name(x.tag).casefold() == name.casefold()]


def safe_parse(path: Path) -> tuple[ET.Element | None, str | None]:
    try:
        # ElementTree does not resolve external entities; DTDs are not expanded.
        return ET.parse(path).getroot(), None
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def stable_json_hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


@dataclass
class ModuleInfo:
    folder: str
    path: str
    module_id: str
    name: str
    version: str
    enabled: bool = True
    load_order: int | None = None
    enabled_known: bool = False
    load_order_source: str = "unknown"
    is_official: bool = False
    is_singleplayer_module: bool = False
    is_multiplayer_module: bool = False
    is_server_module: bool = False
    excluded_by_game_mode: str | None = None
    dependencies: list[dict[str, Any]] = field(default_factory=list)
    dlls: list[str] = field(default_factory=list)
    load_after_this: list[str] = field(default_factory=list)
    ordering_metadata: list[dict[str, str]] = field(default_factory=list)
    incompatible_modules: list[str] = field(default_factory=list)
    xml_registrations: list[dict[str, str]] = field(default_factory=list)
    xml_files: list[str] = field(default_factory=list)
    xslt_files: list[str] = field(default_factory=list)
    resource_files: list[str] = field(default_factory=list)
    parse_errors: list[str] = field(default_factory=list)
    fingerprint: str = ""


@dataclass(frozen=True)
class ModuleFileInventory:
    """File lists collected in one directory walk for a module."""

    all_files: tuple[Path, ...]
    xml_files: tuple[Path, ...]
    xsd_files: tuple[Path, ...]
    xslt_files: tuple[Path, ...]
    resource_files: tuple[Path, ...]


@dataclass
class XmlRecord:
    module_id: str
    module_name: str
    table: str
    tag: str
    key: str
    attributes: dict[str, str]
    source: str
    key_confidence: str


@dataclass
class Finding:
    finding_id: str
    category: str
    severity: str
    title: str
    description: str
    modules: list[str]
    evidence: dict[str, Any] = field(default_factory=dict)
    risk_points: int = 0
    confidence: str = "Medium"
    recommendation: str = ""


def _compare_mod_xml_records(
    modded: list[ModuleInfo],
    records_by_module: dict[str, dict[tuple[str, str, str], XmlRecord]],
    baseline_records: dict[tuple[str, str, str], dict[str, str]],
    performance_stats: dict[str, int],
    load_order_constraints: list[dict[str, Any]] | None = None,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Compare only module pairs that share XML object keys.

    The previous implementation intersected every pair of modules' complete
    record-key dictionaries. This index groups the (already de-duplicated)
    module records by XML object first, then visits the module combinations
    that actually contain that object. The comparison rules remain pairwise.
    """
    first_record_by_object: dict[tuple[str, str, str], XmlRecord] = {}
    shared_records_by_object: dict[tuple[str, str, str], list[tuple[int, XmlRecord]]] = {}
    pair_key_cache: dict[int, tuple[str, str]] = {}
    module_count = len(modded)
    module_index_by_id: dict[str, int] = {}
    for module_index, module in enumerate(modded):
        module_index_by_id.setdefault(module.module_id, module_index)

    for module_index, module in enumerate(modded):
        for object_key, record in records_by_module.get(module.module_id, {}).items():
            first_record = first_record_by_object.get(object_key)
            if first_record is None:
                first_record_by_object[object_key] = record
                continue

            shared_records = shared_records_by_object.get(object_key)
            if shared_records is None:
                first_module_index = module_index_by_id[first_record.module_id]
                shared_records_by_object[object_key] = [
                    (first_module_index, first_record),
                    (module_index, record),
                ]
            else:
                shared_records.append((module_index, record))

    # Release the one-off object index before comparing shared objects. Most
    # XML keys in large collections are unique, so do not allocate a list for
    # each of them or keep their temporary index alive during pair comparison.
    del first_record_by_object

    # Keep the historical counter's meaning: total possible pairs among
    # enabled third-party modules, whether or not their XML inventories overlap.
    performance_stats["mod_pairs_considered"] += module_count * (module_count - 1) // 2

    pair_acc: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        lambda: {
            "contested": [], "same_write": [], "objects": set(), "risk": 0,
            "relationship": None, "compatibility_patch_candidates": [],
        }
    )

    for object_key, entries in shared_records_by_object.items():
        base_attrs = baseline_records.get(object_key, {})
        for index, (module_index_a, record_a) in enumerate(entries[:-1]):
            attrs_a = record_a.attributes
            for other_index in range(index + 1, len(entries)):
                module_index_b, record_b = entries[other_index]
                performance_stats["shared_xml_objects_compared"] += 1
                # Cache the canonical module ID pair only when an actual XML
                # overlap exists. Integer keys avoid allocating a tuple for
                # every shared object in dense-overlap mod collections.
                pair_code = module_index_a * module_count + module_index_b
                pair = pair_key_cache.get(pair_code)
                if pair is None:
                    pair = tuple(sorted(
                        (modded[module_index_a].module_id, modded[module_index_b].module_id),
                        key=str.casefold,
                    ))
                    pair_key_cache[pair_code] = pair
                acc = pair_acc[pair]

                attrs_b = record_b.attributes
                contested = []
                for attr in attrs_a.keys() & attrs_b.keys():
                    value_a, value_b = attrs_a[attr], attrs_b[attr]
                    if value_a == value_b:
                        if base_attrs.get(attr) not in (None, value_a):
                            acc["same_write"].append({
                                "table": record_a.table,
                                "tag": record_a.tag,
                                "key": record_a.key,
                                "attribute": attr,
                                "value": value_a,
                            })
                        continue

                    base_value = base_attrs.get(attr)
                    # Only call it a competing override if both mods actually
                    # differ from official baseline, or no baseline exists.
                    both_changed = (base_value is None) or (
                        value_a != base_value and value_b != base_value
                    )
                    if both_changed:
                        item = {
                            "table": record_a.table,
                            "tag": record_a.tag,
                            "key": record_a.key,
                            "attribute": attr,
                            "baseline": base_value,
                            "mod_a_value": value_a,
                            "mod_b_value": value_b,
                            "source_a": record_a.source,
                            "source_b": record_b.source,
                            "key_confidence": (
                                "XSD" if record_a.key_confidence == record_b.key_confidence == "XSD"
                                else "Heuristic"
                            ),
                        }
                        contested.append(item)
                        acc["contested"].append(item)

                if contested:
                    # Store the same normalized object key used by records_by_module;
                    # compatibility-patch candidate matching intersects these sets.
                    acc["objects"].add(object_key)

    performance_stats["mod_pairs_with_shared_xml"] += len(pair_key_cache)

    # A declared direct relationship can explain why a dependent add-on and its
    # base module write different values. It is evidence of intended order, not
    # proof that the resulting game data is functionally correct.
    direct_edges: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for edge in load_order_constraints or []:
        before_id = norm(str(edge.get("before_id", "")))
        after_id = norm(str(edge.get("after_id", "")))
        if before_id and after_id and before_id != after_id:
            direct_edges[(before_id, after_id)].append(edge)

    for pair, acc in pair_acc.items():
        left_id, right_id = norm(pair[0]), norm(pair[1])
        left_to_right = direct_edges.get((left_id, right_id), [])
        right_to_left = direct_edges.get((right_id, left_id), [])
        if bool(left_to_right) == bool(right_to_left):
            continue
        edge = (left_to_right or right_to_left)[0]
        source = str(edge.get("source", ""))
        relationship = {
            "before_id": str(edge.get("before_id", "")),
            "after_id": str(edge.get("after_id", "")),
            "source": source,
            "strength": str(edge.get("strength", "")),
        }
        if source == "DependedModules" and relationship["strength"] == "Required":
            relationship["classification"] = "addon_override_candidate"
            relationship["description"] = (
                f"{relationship['after_id']} объявляет обязательную зависимость от "
                f"{relationship['before_id']}; направление переопределения согласуется с этим отношением."
            )
        else:
            relationship["classification"] = "declared_ordered_overlap"
            relationship["description"] = (
                f"Метаданные {source or 'модуля'} явно задают направление "
                f"{relationship['before_id']} → {relationship['after_id']}."
            )
        acc["relationship"] = relationship

    # A third module is only a *candidate* compatibility patch when it declares
    # required dependencies on both sides and actually edits at least one of the
    # same contested XML objects. Dependency on two modules alone is not enough.
    for (pair_left_id, pair_right_id), acc in pair_acc.items():
        left_id, right_id = norm(pair_left_id), norm(pair_right_id)
        contested_objects = acc["objects"]
        if not contested_objects:
            continue
        for candidate in modded:
            candidate_id = norm(candidate.module_id)
            if candidate_id in {left_id, right_id}:
                continue
            required_dependencies = {
                norm(str(dep.get("id", "")))
                for dep in candidate.dependencies
                if not dep.get("optional") and str(dep.get("id", "")).strip()
            }
            if not {left_id, right_id}.issubset(required_dependencies):
                continue
            candidate_objects = set(records_by_module.get(candidate.module_id, {}))
            shared_objects = contested_objects & candidate_objects
            if shared_objects:
                acc["compatibility_patch_candidates"].append({
                    "module_id": candidate.module_id,
                    "name": candidate.name,
                    "shared_contested_objects": len(shared_objects),
                    "classification": "candidate_not_confirmed",
                })

    return pair_acc


def parse_bem_profile(path: Path | None) -> dict[str, dict[str, Any]]:
    """Read a user-selected load-order JSON, preserving unknown enabled states."""
    if not path:
        return {}
    return parse_load_order_json(path, strict=True)


def _registered_xmls(root: ET.Element) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    for xml_node in root.iter():
        if local_name(xml_node.tag).casefold() != "xmlnode":
            continue
        for child in xml_node.iter():
            if local_name(child.tag).casefold() == "xmlname":
                table_id = child.attrib.get("id", "").strip()
                path = child.attrib.get("path", "").strip()
                if table_id or path:
                    found.append({"id": table_id, "path": path})
    return found


def _read_manifest(module_dir: Path, bem_entry: dict[str, Any] | None) -> ModuleInfo | None:
    manifest_path = module_dir / "SubModule.xml"
    if not manifest_path.exists():
        return None
    root, error = safe_parse(manifest_path)
    if root is None:
        return ModuleInfo(
            folder=module_dir.name, path=str(module_dir), module_id=module_dir.name,
            name=module_dir.name, version="?", parse_errors=[f"SubModule.xml: {error}"]
        )

    module_id = child_value(root, "Id", module_dir.name).strip()
    name = child_value(root, "Name", module_id).strip()
    version = child_value(root, "Version", "?").strip()
    dependencies: list[dict[str, Any]] = []
    dep_parent = next((x for x in root.iter() if local_name(x.tag).casefold() == "dependedmodules"), None)
    for dep in (list(dep_parent) if dep_parent is not None else []):
        if local_name(dep.tag).casefold() not in {"dependedmodule", "module"}:
            continue
        dep_id = dep.attrib.get("Id") or dep.attrib.get("id") or child_value(dep, "Id", "")
        if dep_id:
            optional = dep.attrib.get("Optional") or dep.attrib.get("optional")
            dependency_version = dep.attrib.get("DependentVersion") or dep.attrib.get("dependentVersion") or ""
            dependencies.append({
                "id": dep_id,
                "optional": str(optional).casefold() in {"true", "1"},
                "version": dependency_version,
            })

    load_after_this: list[str] = []
    after_parent = next((x for x in root.iter() if local_name(x.tag).casefold() == "modulestoloadafterthis"), None)
    for node in (list(after_parent) if after_parent is not None else []):
        target_id = node.attrib.get("Id") or node.attrib.get("id") or node.attrib.get("value") or child_value(node, "Id", "")
        if target_id:
            load_after_this.append(target_id)

    ordering_metadata: list[dict[str, str]] = []
    # Parse metadata entries directly, whether or not the game manifest wraps them
    # in DependedModuleMetadatas / DependentModuleMetadatas.
    metadata_nodes = [
        node for node in root.iter()
        if local_name(node.tag).casefold() in {"dependedmodulemetadata", "dependentmodulemetadata"}
    ]
    for node in metadata_nodes:
        target_id = node.attrib.get("id") or node.attrib.get("Id") or child_value(node, "Id", "")
        order = (node.attrib.get("order") or node.attrib.get("loadType") or
                 node.attrib.get("LoadType") or child_value(node, "Order", ""))
        if target_id and order:
            ordering_metadata.append({
                "id": target_id,
                "order": order,
                "optional": str(node.attrib.get("isOptional", node.attrib.get("optional", "false"))).casefold() in {"true", "1"},
                "incompatible": str(node.attrib.get("isIncompatible", node.attrib.get("incompatible", "false"))).casefold() in {"true", "1"},
                "version": node.attrib.get("version", ""),
            })

    incompatible_modules: list[str] = []
    incompatible_parent = next((x for x in root.iter() if local_name(x.tag).casefold() == "incompatiblemodules"), None)
    for node in (list(incompatible_parent) if incompatible_parent is not None else []):
        target_id = node.attrib.get("Id") or node.attrib.get("id") or child_value(node, "Id", "")
        if target_id:
            incompatible_modules.append(target_id)

    dlls: list[str] = []
    for el in root.iter():
        if local_name(el.tag).casefold() == "dllname":
            value = (el.text or "").strip() or el.attrib.get("value", "").strip()
            if value:
                dlls.append(value)

    bem_entry = bem_entry or {}
    is_official = bool(bem_entry.get("is_official", False)) or norm(module_id) in OFFICIAL_FOLDER_IDS or norm(module_dir.name) in OFFICIAL_FOLDER_IDS
    module_category = norm(child_value(root, "ModuleCategory", ""))
    is_singleplayer_module = (
        parse_optional_bool(child_value(root, "SingleplayerModule", "")) is True
        or module_category in {"singleplayer", "singleplayeroptional"}
    )
    is_multiplayer_module = (
        parse_optional_bool(child_value(root, "MultiplayerModule", "")) is True
        or module_category in {"multiplayer", "multiplayeroptional"}
        or norm(module_id) in NETWORK_ONLY_MODULE_IDS
    )
    is_server_module = (
        module_category in {"server", "serveroptional"}
        or norm(module_id) == "dedicatedcustomserverteam"
    )
    load_order = bem_entry.get("index")
    try:
        load_order = int(load_order) if load_order is not None else None
    except (TypeError, ValueError):
        load_order = None

    return ModuleInfo(
        folder=module_dir.name,
        path=str(module_dir),
        module_id=module_id,
        name=bem_entry.get("name") or name,
        version=version,
        enabled=bool(bem_entry.get("enabled", True)),
        load_order=load_order,
        enabled_known=bool(bem_entry.get("enabled_known", bool(bem_entry))),
        load_order_source=str(bem_entry.get("source_type", "profile" if bem_entry else "unknown")),
        is_official=is_official,
        is_singleplayer_module=is_singleplayer_module,
        is_multiplayer_module=is_multiplayer_module,
        is_server_module=is_server_module,
        dependencies=dependencies,
        dlls=sorted(set(dlls), key=str.casefold),
        load_after_this=load_after_this,
        ordering_metadata=ordering_metadata,
        incompatible_modules=incompatible_modules,
        xml_registrations=_registered_xmls(root),
    )


def _resolve_table_key(xml_path: Path, module_dir: Path, registrations: list[dict[str, str]]) -> str:
    rel = xml_path.relative_to(module_dir).as_posix()
    rel_norm = rel.replace("\\", "/").casefold()
    stem = xml_path.stem.casefold()
    for registration in registrations:
        reg_path = registration.get("path", "").replace("\\", "/").strip("/").casefold()
        if not reg_path:
            continue
        reg_stem = Path(reg_path).stem
        if reg_path == rel_norm or reg_path == rel_norm.removeprefix("moduledata/") or reg_stem == stem:
            return registration.get("id") or stem
    # Prefer path identity when the XML is in a non-standard subfolder. This avoids
    # treating unrelated files with the same basename as the same table.
    parts = [p for p in rel_norm.split("/") if p.casefold() != "moduledata"]
    return "/".join(parts) if len(parts) > 1 else stem


def _collect_module_files(module_dir: Path) -> ModuleFileInventory:
    """Walk a module directory once and classify files for all scan stages."""
    all_files: list[Path] = []
    xml_files: list[Path] = []
    xsd_files: list[Path] = []
    xslt_files: list[Path] = []
    resource_files: list[Path] = []
    module_data = module_dir / "ModuleData"
    excluded_xml_parts = {"xmlschemas", "schemas", "translations", "languages"}
    fingerprint_excluded_resource_exts = {".xml", ".xsl", ".xslt", ".dll", ".txt", ".json"}

    for path in module_dir.rglob("*"):
        if not path.is_file():
            continue
        all_files.append(path)
        suffix = path.suffix.casefold()

        if suffix == ".xsd":
            xsd_files.append(path)
        elif suffix in {".xsl", ".xslt"}:
            xslt_files.append(path)

        if suffix == ".xml" and path.is_relative_to(module_data):
            if not any(part.casefold() in excluded_xml_parts for part in path.parts):
                xml_files.append(path)

        try:
            relative = path.relative_to(module_dir).as_posix()
            parts = [part.casefold() for part in Path(relative).parts]
        except ValueError:
            continue
        # Vortex deployment marker files are metadata, not game resources.
        if path.name.casefold() == "__folder_managed_by_vortex":
            continue
        if any(part in RESOURCE_DIR_HINTS for part in parts) and suffix not in fingerprint_excluded_resource_exts:
            resource_files.append(path)

    return ModuleFileInventory(
        all_files=tuple(sorted(all_files)),
        xml_files=tuple(sorted(xml_files)),
        xsd_files=tuple(xsd_files),
        xslt_files=tuple(sorted(xslt_files)),
        resource_files=tuple(resource_files),
    )


def _load_xsd_unique_attributes(
    modules: list[ModuleInfo],
    inventories: dict[str, ModuleFileInventory] | None = None,
) -> dict[tuple[str, str], list[str]]:
    """Best-effort XSD reader for xs:unique / xs:key selector and field metadata."""
    result: dict[tuple[str, str], set[str]] = defaultdict(set)
    xs_ns = "{http://www.w3.org/2001/XMLSchema}"
    for module in modules:
        module_dir = Path(module.path)
        inventory = inventories.get(module.path) if inventories is not None else None
        xsd_paths = inventory.xsd_files if inventory is not None else module_dir.rglob("*.xsd")
        for xsd in xsd_paths:
            try:
                root = ET.parse(xsd).getroot()
            except Exception:
                continue
            table = xsd.stem.casefold()
            for unique in root.iter():
                if local_name(unique.tag).casefold() not in {"unique", "key"}:
                    continue
                selector = next((x for x in unique if local_name(x.tag).casefold() == "selector"), None)
                if selector is None:
                    continue
                xpath = selector.attrib.get("xpath", "").strip()
                tag = xpath.split("/")[-1].replace(".", "").strip()
                if not tag or tag == "*":
                    continue
                for field_el in unique:
                    if local_name(field_el.tag).casefold() != "field":
                        continue
                    field_xpath = field_el.attrib.get("xpath", "").strip()
                    if field_xpath.startswith("@"):
                        result[(table, norm(tag))].add(field_xpath[1:])
    return {k: sorted(v) for k, v in result.items()}


def _flatten_record(element: ET.Element, key_attrs: list[str]) -> dict[str, str]:
    """Flatten an XML record using stable child identities where available.

    Positional indexes are retained only for anonymous repeated children. Named
    collection entries (equipment slots, skills, keyed records, etc.) keep the
    same path when their order changes.
    """
    values: dict[str, str] = {}
    for attr, value in element.attrib.items():
        if attr not in key_attrs:
            values[f"@{attr}"] = value

    identity_attributes = ("id", "skill_id", "key", "name", "slot", "type")

    def selector_value(value: str) -> str:
        # This path is a stable internal key, not an XPath expression executed
        # against user input. JSON quoting gives deterministic escaping.
        return json.dumps(value, ensure_ascii=False)

    def walk(parent: ET.Element, prefix: str) -> None:
        anonymous_counts: dict[str, int] = defaultdict(int)
        identity_counts: dict[tuple[str, tuple[tuple[str, str], ...]], int] = defaultdict(int)
        for child in list(parent):
            tag = local_name(child.tag)
            identity_attr = next(
                (attr for attr in identity_attributes if child.attrib.get(attr, "").strip()),
                None,
            )
            if identity_attr is not None:
                # Use one stable identity, not every descriptive attribute. If a
                # mod changes @name while @id remains the same, the XML object is
                # still the same object and the name change must be compared.
                identity_value = child.attrib[identity_attr]
                identity = ((identity_attr, identity_value),)
                identity_key = (tag, identity)
                identity_counts[identity_key] += 1
                predicate = f"@{identity_attr}={selector_value(identity_value)}"
                suffix = f"{tag}[{predicate}]"
                if identity_counts[identity_key] > 1:
                    suffix += f"[{identity_counts[identity_key]}]"
            else:
                anonymous_counts[tag] += 1
                suffix = f"{tag}[{anonymous_counts[tag]}]"
            child_path = f"{prefix}/{suffix}"
            for attr, value in child.attrib.items():
                values[f"{child_path}/@{attr}"] = value
            text = (child.text or "").strip()
            if text and not list(child):
                values[f"{child_path}/#text"] = text
            walk(child, child_path)
    walk(element, ".")
    text = (element.text or "").strip()
    if text and not list(element):
        values["#text"] = text
    return values


def _extract_records(
    module: ModuleInfo,
    xsd_keys: dict[tuple[str, str], list[str]],
    xml_paths: Iterable[Path] | None = None,
) -> tuple[list[XmlRecord], list[str]]:
    module_dir = Path(module.path)
    records: list[XmlRecord] = []
    errors: list[str] = []
    module_data = module_dir / "ModuleData"
    if not module_data.exists():
        return records, errors
    paths = xml_paths if xml_paths is not None else sorted(module_data.rglob("*.xml"))
    for path in paths:
        source_path = str(path.relative_to(module_dir))
        root, err = safe_parse(path)
        if root is None:
            errors.append(f"{source_path}: {err}")
            continue
        module.xml_files.append(source_path)
        table = _resolve_table_key(path, module_dir, module.xml_registrations)
        for element in root.iter():
            tag = local_name(element.tag)
            # XSD unique keys first; fallback to common id/name heuristic.
            unique_attrs = xsd_keys.get((norm(table), norm(tag)), [])
            key_confidence = "XSD" if unique_attrs else "Heuristic"
            if not unique_attrs:
                if "id" in element.attrib:
                    unique_attrs = ["id"]
                elif "name" in element.attrib:
                    unique_attrs = ["name"]
                else:
                    continue
            if not all(a in element.attrib for a in unique_attrs):
                continue
            key_payload = {a: element.attrib[a] for a in unique_attrs}
            key = json.dumps(key_payload, sort_keys=True, ensure_ascii=False)
            records.append(XmlRecord(
                module_id=module.module_id,
                module_name=module.name,
                table=table,
                tag=tag,
                key=key,
                attributes=_flatten_record(element, unique_attrs),
                source=source_path,
                key_confidence=key_confidence,
            ))
    return records, errors


def _scan_xslt(module: ModuleInfo, xslt_paths: Iterable[Path] | None = None) -> list[dict[str, Any]]:
    results = []
    module_dir = Path(module.path)
    paths = xslt_paths if xslt_paths is not None else sorted(list(module_dir.rglob("*.xsl")) + list(module_dir.rglob("*.xslt")))
    for path in paths:
        try:
            root = ET.parse(path).getroot()
            operations = []
            for element in root.iter():
                lname = local_name(element.tag).casefold()
                if lname in {"template", "apply-templates", "value-of", "copy-of", "for-each", "key"}:
                    for attr in ("match", "select", "test", "name"):
                        val = element.attrib.get(attr)
                        if val:
                            operations.append({"element": lname, "attribute": attr, "expression": val[:500]})
            module.xslt_files.append(str(path.relative_to(module_dir)))
            results.append({"path": str(path.relative_to(module_dir)), "operations": operations[:300]})
        except Exception as exc:
            results.append({"path": str(path.relative_to(module_dir)), "error": str(exc)})
    return results


def _resource_inventory(
    module: ModuleInfo,
    resource_paths: Iterable[Path] | None = None,
) -> dict[str, tuple[str, str]]:
    """Return relative path -> (sha256, source path) for likely overriding resources."""
    module_dir = Path(module.path)
    inventory: dict[str, tuple[str, str]] = {}
    paths = resource_paths if resource_paths is not None else _collect_module_files(module_dir).resource_files
    for path in paths:
        if not path.is_file():
            continue
        rel = path.relative_to(module_dir).as_posix()
        # Hash only files with identical relative resource paths across mods.
        inventory[rel.casefold()] = ("", rel)
    return inventory


def _normalized_file_path(path: Path) -> str:
    try:
        return os.path.normcase(str(path.expanduser().resolve(strict=False))).casefold()
    except (OSError, RuntimeError, ValueError):
        return os.path.normcase(os.path.abspath(os.path.normpath(str(path)))).casefold()


def _is_network_only_module(module: ModuleInfo) -> bool:
    return (
        module.is_multiplayer_module
        or module.is_server_module
        or norm(module.module_id) in NETWORK_ONLY_MODULE_IDS
    )


def _apply_profile_game_mode(modules: list[ModuleInfo], game_mode: str | None) -> list[ModuleInfo]:
    """Return modules in the selected mode without guessing network-only activation.

    For a confirmed Singleplayer profile, official multiplayer/server modules
    are outside the graph unless that profile explicitly marks them enabled.
    Other modes and unknown mode retain the conservative existing behavior.
    """
    scoped: list[ModuleInfo] = []
    for module in modules:
        module.excluded_by_game_mode = None
        if (
            game_mode == "singleplayer"
            and _is_network_only_module(module)
            and not (module.enabled_known and module.enabled)
        ):
            module.excluded_by_game_mode = "singleplayer"
            continue
        scoped.append(module)
    return scoped


def _make_fingerprint(
    module: ModuleInfo,
    file_paths: Iterable[Path] | None = None,
    stats: dict[str, int] | None = None,
    hash_cache: dict[str, tuple[int, int, str]] | None = None,
    pending_hashes: dict[str, tuple[int, int, str]] | None = None,
    force_rehash: bool = False,
) -> str:
    """Fingerprint module files, reusing SHA-256 values when stat data is unchanged.

    XML/XSLT/DLL and SubModule.xml are content-hashed. Other files retain the
    existing relative-path/size/mtime fingerprint. The persistent cache is an
    optimization only: force_rehash bypasses it for a deep verification.
    """
    module_dir = Path(module.path)
    items: list[tuple[str, str]] = []
    paths = file_paths if file_paths is not None else sorted(x for x in module_dir.rglob("*") if x.is_file())
    for path in paths:
        try:
            rel = path.relative_to(module_dir).as_posix().casefold()
            stat = path.stat()
            ext = path.suffix.casefold()
            should_hash = rel == "submodule.xml" or ext in {".xml", ".xsl", ".xslt", ".dll"}
            if should_hash:
                cache_key = _normalized_file_path(path)
                cached = hash_cache.get(cache_key) if hash_cache is not None and not force_rehash else None
                if cached is not None and cached[0] == stat.st_size and cached[1] == stat.st_mtime_ns:
                    digest = cached[2]
                    if stats is not None:
                        stats["fingerprint_cache_hits"] = stats.get("fingerprint_cache_hits", 0) + 1
                else:
                    digest = sha256_file(path)
                    if stats is not None:
                        stats["fingerprinted_files"] = stats.get("fingerprinted_files", 0) + 1
                        stats["bytes_hashed"] = stats.get("bytes_hashed", 0) + stat.st_size
                        stats["fingerprint_cache_misses"] = stats.get("fingerprint_cache_misses", 0) + 1
                    if pending_hashes is not None:
                        pending_hashes[cache_key] = (stat.st_size, stat.st_mtime_ns, digest)
                value = digest
            else:
                value = f"{stat.st_size}:{stat.st_mtime_ns}"
            items.append((rel, value))
        except OSError:
            if stats is not None:
                stats["fingerprint_unreadable_files"] = stats.get("fingerprint_unreadable_files", 0) + 1
            continue
    return stable_json_hash(items)


def _emit_progress(callback: Callable[[dict[str, Any]], None] | None, stage: str, label: str,
                   current: int, total: int, percent: int, detail: str = "") -> None:
    if callback is None:
        return
    try:
        callback({
            "type": "progress", "stage": stage, "label": label,
            "current": current, "total": total, "percent": max(0, min(100, int(percent))),
            "detail": detail,
        })
    except Exception:
        # UI/progress reporting must never break a compatibility scan.
        pass


_VERSION_CHANNEL_ORDER = {"a": 0, "b": 1, "e": 2, "v": 3, "d": 4}
_VERSION_MAX_COMPONENT = 2_147_483_647


def _parse_version_bound(value: str, *, upper: bool = False) -> tuple[int, int, int, int, int] | None:
    """Parse a Bannerlord ApplicationVersion into BUTR-compatible sort fields."""
    text = str(value or "").strip().casefold()
    if not text:
        return None
    prefix = text[0] if text[0] in _VERSION_CHANNEL_ORDER else "v"
    numeric = text[1:] if text[0] in _VERSION_CHANNEL_ORDER else text
    parts = numeric.split(".")
    if len(parts) < 3 or len(parts) > 4:
        return None
    components: list[int] = []
    wildcard_seen = False
    for part in parts:
        if part == "*":
            wildcard_seen = True
            components.append(_VERSION_MAX_COMPONENT if upper else 0)
            continue
        if wildcard_seen or not part.isdigit():
            return None
        try:
            number = int(part)
        except ValueError:
            return None
        if number < 0 or number > _VERSION_MAX_COMPONENT:
            return None
        components.append(number)
    while len(components) < 4:
        # BUTR parses the lower endpoint with omitted components set to zero
        # and the upper endpoint with omitted components set to Int32.MaxValue.
        components.append(_VERSION_MAX_COMPONENT if upper else 0)
    return (_VERSION_CHANNEL_ORDER[prefix], *components[:4])


def _parse_version_constraint(expected: str) -> tuple[tuple[int, ...] | None, tuple[int, ...] | None, bool, bool] | None:
    """Return lower/upper bounds and inclusivity for BUTR and NuGet constraints."""
    text = str(expected or "").strip()
    if not text:
        return None

    # NuGet interval notation uses exact endpoints and explicit inclusivity.
    # Unlike BUTR's ApplicationVersionRange, omitted components here are not
    # expanded to Int32.MaxValue.
    if len(text) >= 2 and text[0] in "[(" and text[-1] in ")]" and "," in text:
        lower_text, upper_text = (part.strip() for part in text[1:-1].split(",", 1))
        lower = _parse_version_bound(lower_text) if lower_text else None
        upper = _parse_version_bound(upper_text) if upper_text else None
        if (lower_text and lower is None) or (upper_text and upper is None):
            return None
        return lower, upper, text[0] == "[", text[-1] == "]"

    # BUTR ApplicationVersionRange uses "minimum - maximum"; its parser pads
    # omitted components in the upper endpoint with Int32.MaxValue. Both ends
    # are inclusive in BUTR's validator.
    if "-" in text:
        # BUTR removes whitespace before splitting ApplicationVersionRange, so
        # accept both "v1.0.0 - v2.0.0" and "v1.0.0-v2.0.0".
        lower_text, upper_text = (part.strip() for part in text.split("-", 1))
        lower = _parse_version_bound(lower_text)
        upper = _parse_version_bound(upper_text, upper=True)
        if lower is None or upper is None:
            return None
        return lower, upper, True, True

    # BUTR's ApplicationVersion.TryParse accepts wildcard components by turning
    # them into a minimum version (e.g. v1.4.0.* => >= v1.4.0.0). The BUTR
    # validator does not enforce the wildcard prefix as an upper bound.
    lower = _parse_version_bound(text)
    if lower is None:
        return None
    return lower, None, True, True


def _normalized_version_constraint(expected: str) -> str:
    """Return a semantic key for equivalent BUTR/NuGet version constraints."""
    parsed = _parse_version_constraint(expected)
    if parsed is None:
        return "raw:" + re.sub(r"\s+", "", str(expected or "")).casefold()
    lower, upper, lower_inclusive, upper_inclusive = parsed
    payload = {
        "lower": list(lower) if lower is not None else None,
        "upper": list(upper) if upper is not None else None,
        "lower_inclusive": bool(lower_inclusive),
        "upper_inclusive": bool(upper_inclusive),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _version_constraint_matches(actual: str, expected: str, *, allow_wildcards: bool = True) -> str:
    """Return a structured version result: satisfied, vanilla_mismatch_only,
    incompatible, or unknown.

    BUTR treats a single declared version and its wildcard forms as minimum
    versions. The explicit BUTR "min - max" range is inclusive; NuGet interval
    notation uses the endpoint inclusivity shown by its brackets. Version
    channels (a/b/e/v/d) retain their BUTR ordering.
    """
    actual_text = str(actual or "").strip()
    expected_text = str(expected or "").strip()
    if not actual_text or actual_text == "?" or "*" in actual_text or not expected_text:
        return "unknown"
    if not allow_wildcards and "*" in expected_text:
        return "unknown"

    actual_version = _parse_version_bound(actual_text)
    constraint = _parse_version_constraint(expected_text)
    if actual_version is None or constraint is None:
        return "unknown"
    lower, upper, lower_inclusive, upper_inclusive = constraint

    if lower is not None:
        if actual_version < lower or (actual_version == lower and not lower_inclusive):
            return "incompatible"
    if upper is not None:
        if actual_version > upper or (actual_version == upper and not upper_inclusive):
            return "incompatible"

    # A plain exact textual match needs no compatibility note. A newer version,
    # an equivalent normalized version written differently, or a version accepted
    # by a range/wildcard is valid for BUTR but can still produce a vanilla
    # launcher's stricter version warning.
    if actual_text.casefold() == expected_text.casefold():
        return "satisfied"
    return "vanilla_mismatch_only"


def analyze_load_order(modules: list[ModuleInfo], order_source: str | None = None) -> dict[str, Any]:
    """Build declared ordering constraints and a stable topological recommendation.

    This recommends the list a launcher should persist. It does not claim that the
    single-player game executable automatically sorts modules at runtime.
    Ambiguous Module IDs are kept visible, but prevent the recommendation being
    treated as safe to apply.
    """
    active = [m for m in modules if m.enabled]
    active_state_known = bool(modules) and all(m.enabled_known for m in modules)

    # Use a unique internal node key for every module folder. Module ID remains the
    # reference key in SubModule.xml, but it is not guaranteed to be unique on disk.
    node_to_module: dict[str, ModuleInfo] = {}
    module_to_node: dict[int, str] = {}
    active_by_id: dict[str, list[str]] = defaultdict(list)
    installed_by_id: dict[str, list[ModuleInfo]] = defaultdict(list)
    for module in modules:
        installed_by_id[norm(module.module_id)].append(module)
    for index, module in enumerate(active):
        node_key = f"{norm(module.module_id)}::{index}"
        node_to_module[node_key] = module
        module_to_node[id(module)] = node_key
        active_by_id[norm(module.module_id)].append(node_key)

    ambiguous_module_ids = {
        key: [module.folder for module in installed_by_id.get(key, [])]
        for key in active_by_id
        if len(installed_by_id.get(key, [])) > 1
    }
    constraints: list[dict[str, Any]] = []
    constraint_nodes: list[tuple[str, str]] = []
    issues: list[dict[str, Any]] = []
    notes: list[str] = []
    edge_set: set[tuple[str, str]] = set()
    issue_keys: set[tuple[str, str, str, str]] = set()

    blocking_reasons: list[str] = []
    if ambiguous_module_ids:
        for module_id, folders in sorted(ambiguous_module_ids.items()):
            blocking_reasons.append(
                f"Module ID '{module_id}' встречается в нескольких папках: {', '.join(folders)}"
            )

    def add_edge(before: ModuleInfo, after: ModuleInfo, reason: str, source: str, strength: str) -> None:
        before_key = module_to_node[id(before)]
        after_key = module_to_node[id(after)]
        key = (before_key, after_key)
        if key in edge_set:
            return
        edge_set.add(key)
        constraints.append({
            "before_id": before.module_id,
            "after_id": after.module_id,
            "reason": reason,
            "source": source,
            "strength": strength,
        })
        constraint_nodes.append(key)

    def add_ambiguous_reference(source_module: ModuleInfo, target_id: str, source: str) -> None:
        key = ("ambiguous_module_reference", norm(source_module.module_id), norm(target_id), source)
        if key in issue_keys:
            return
        issue_keys.add(key)
        issues.append({
            "kind": "ambiguous_module_reference",
            "severity": "Critical" if active_state_known else "High",
            "modules": [source_module.module_id, target_id],
            "target": target_id,
            "description": (
                f"{source_module.name} ссылается на {target_id}, но этот Module ID принадлежит нескольким папкам. "
                "BCI не выбирает одну копию произвольно, поэтому соответствующее правило не добавлено в граф."
            ),
            "source": source,
        })

    version_issue_records: dict[tuple[str, str, str], dict[str, Any]] = {}

    def add_version_issue(
        kind: str, module: ModuleInfo, target: ModuleInfo, expected: str, source: str, *, optional: bool = False
    ) -> None:
        expected_text = str(expected or "").strip()
        if not expected_text:
            return
        version_status = _version_constraint_matches(
            target.version, expected_text, allow_wildcards=True
        )
        normalized_constraint = _normalized_version_constraint(expected_text)
        issue_key = (norm(module.module_id), norm(target.module_id), normalized_constraint)
        record = version_issue_records.get(issue_key)
        if record is None:
            record = {
                "module": module,
                "target": target,
                "normalized_constraint": normalized_constraint,
                "expected_versions": [],
                "sources": [],
                "statuses": [],
                "optional_flags": [],
            }
            version_issue_records[issue_key] = record
        rule = {"source": source, "expected_version": expected_text, "optional": bool(optional)}
        if rule not in record["expected_versions"]:
            record["expected_versions"].append(rule)
        if source not in record["sources"]:
            record["sources"].append(source)
        record["statuses"].append(version_status)
        record["optional_flags"].append(bool(optional))

    def finalize_version_issues() -> None:
        """Emit one finding per source/target/semantically identical constraint."""
        status_priority = {
            "satisfied": 0,
            "vanilla_mismatch_only": 1,
            "unknown": 2,
            "incompatible": 3,
        }
        for record in version_issue_records.values():
            statuses = record["statuses"]
            version_status = max(statuses, key=lambda item: status_priority.get(item, 2))
            if version_status == "satisfied":
                continue

            module: ModuleInfo = record["module"]
            target: ModuleInfo = record["target"]
            sources = record["sources"]
            optional = bool(record["optional_flags"]) and all(record["optional_flags"])
            is_dependency_rule = "DependedModules" in sources
            kind_prefix = "dependency_version" if is_dependency_rule else "metadata_version"
            if version_status == "unknown":
                kind = kind_prefix + "_unknown"
            elif version_status == "vanilla_mismatch_only":
                kind = kind_prefix + "_vanilla_mismatch"
            else:
                kind = kind_prefix + "_mismatch"
            expected_versions = record["expected_versions"]
            expected_display = " / ".join(dict.fromkeys(
                item["expected_version"] for item in expected_versions
            ))
            source_display = " + ".join(sources)

            if optional:
                severity = "Info"
                if version_status == "unknown":
                    explanation = (
                        f"Необязательное требование {module.name} к {target.name} ({expected_display}) "
                        "не удалось проверить. Оно не блокирует загрузку модуля."
                    )
                else:
                    explanation = (
                        f"Необязательное требование {module.name} к {target.name} ({expected_display}) "
                        f"не совпадает с установленной версией {target.version}. Модуль необязателен "
                        "для запуска по этому правилу; BCI не считает это несовместимостью и не начисляет риск."
                    )
            elif version_status == "unknown":
                severity = "Info"
                explanation = (
                    f"{module.name} объявляет версионное требование к {target.name} ({expected_display}), "
                    f"но установленную версию нельзя проверить: указано '{target.version or '?'}'."
                )
            elif version_status == "vanilla_mismatch_only":
                severity = "Info"
                explanation = (
                    f"{module.name} требует {target.name} версии/диапазона {expected_display}, "
                    f"установлена {target.version}. Требование BUTR выполнено: установленная версия "
                    "не ниже минимума и находится в допустимом диапазоне. Vanilla Launcher может отдельно "
                    "показать предупреждение из-за отличия записи версии; это не считается несовместимостью BCI."
                )
            else:
                severity = "Medium"
                explanation = (
                    f"{module.name} требует {target.name} версии/диапазона {expected_display}, "
                    f"но установлена версия {target.version}, которая ниже минимума или вне диапазона BUTR. "
                    "Это несоответствие обязательному требованию, но само по себе не доказывает вылет."
                )

            issues.append({
                "kind": kind,
                "severity": severity,
                "modules": [module.module_id, target.module_id],
                "target": target.module_id,
                "expected_version": expected_display,
                "expected_versions": expected_versions,
                "normalized_constraint": record["normalized_constraint"],
                "installed_version": target.version,
                "version_status": version_status,
                "optional": optional,
                "description": explanation,
                "source": source_display,
                "sources": sources,
            })

    # Dependencies and explicit load-after declarations become directed edges.
    for module in active:
        module_key = norm(module.module_id)
        # If multiple active folders claim this ID, their outgoing declarations
        # cannot safely be attributed to the module the launcher will actually load.
        if module_key in ambiguous_module_ids:
            continue

        for dep in module.dependencies:
            dep_id = str(dep.get("id", "")).strip()
            if not dep_id:
                continue
            dep_key = norm(dep_id)
            target_nodes = active_by_id.get(dep_key, [])
            optional = bool(dep.get("optional", False))
            if len(installed_by_id.get(dep_key, [])) > 1:
                add_ambiguous_reference(module, dep_id, "DependedModules")
                if not target_nodes and not optional:
                    issues.append({
                        "kind": "missing_dependency", "severity": "Critical",
                        "modules": [module.module_id], "target": dep_id,
                        "description": (
                            f"{module.name} требует {dep_id}, но ни одна из нескольких установленных копий "
                            "не подтверждена как активная. Сначала устраните дубли и включите одну корректную копию."
                        ),
                        "source": "DependedModules",
                    })
                continue
            if not target_nodes:
                if not optional:
                    installed = installed_by_id.get(dep_key, [])
                    if not installed:
                        description = f"{module.name} требует {dep_id}, но модуль отсутствует среди установленных."
                    else:
                        description = (
                            f"{module.name} требует {dep_id}, но установленная копия не включена в текущий набор."
                        )
                    issues.append({
                        "kind": "missing_dependency", "severity": "Critical",
                        "modules": [module.module_id], "target": dep_id,
                        "description": description, "source": "DependedModules",
                    })
                continue

            dependency = node_to_module[target_nodes[0]]
            add_edge(dependency, module, f"{module.name} зависит от {dependency.name}", "DependedModules", "Required" if not optional else "Optional")
            required_version = str(dep.get("version", "")).strip()
            if required_version:
                add_version_issue("dependency_version_mismatch", module, dependency, required_version, "DependedModules", optional=optional)

        for target_id in module.load_after_this:
            target_key = norm(target_id)
            target_nodes = active_by_id.get(target_key, [])
            if len(installed_by_id.get(target_key, [])) > 1:
                add_ambiguous_reference(module, target_id, "ModulesToLoadAfterThis")
                continue
            if target_nodes:
                target = node_to_module[target_nodes[0]]
                add_edge(module, target, f"{target.name} должен идти после {module.name}", "ModulesToLoadAfterThis", "Declared")

        for metadata in module.ordering_metadata:
            target_id = str(metadata.get("id", "")).strip()
            if not target_id:
                continue
            target_nodes = active_by_id.get(norm(target_id), [])
            if len(installed_by_id.get(norm(target_id), [])) > 1:
                add_ambiguous_reference(module, target_id, "DependedModuleMetadata")
                continue
            if not target_nodes:
                continue
            target = node_to_module[target_nodes[0]]
            metadata_version = str(metadata.get("version", "")).strip()
            if metadata_version:
                add_version_issue(
                    "metadata_version_mismatch", module, target, metadata_version,
                    "DependedModuleMetadata", optional=bool(metadata.get("optional", False)),
                )
            order = norm(metadata.get("order", "")).replace("_", "")
            if order == "loadbeforethis":
                add_edge(target, module, f"{target.name} должен идти перед {module.name}", "DependedModuleMetadata", "Advisory")
            elif order == "loadafterthis":
                add_edge(module, target, f"{module.name} должен идти перед {target.name}", "DependedModuleMetadata", "Advisory")

        for target_id in module.incompatible_modules:
            target_nodes = active_by_id.get(norm(target_id), [])
            if len(installed_by_id.get(norm(target_id), [])) > 1:
                add_ambiguous_reference(module, target_id, "IncompatibleModules")
                continue
            if target_nodes:
                target = node_to_module[target_nodes[0]]
                if norm(module.module_id) < norm(target.module_id):
                    issues.append({
                        "kind": "incompatible_modules", "severity": "Critical" if active_state_known else "Medium",
                        "modules": [module.module_id, target.module_id], "target": target.module_id,
                        "description": (
                            f"{module.name} объявляет {target.name} несовместимым модулем; оба включены."
                            if active_state_known else
                            f"{module.name} объявляет {target.name} несовместимым модулем. Оба модуля установлены, "
                            "но активность не определена — подтвердите, включены ли оба."
                        ),
                        "source": "IncompatibleModules",
                    })

        for metadata in module.ordering_metadata:
            if not metadata.get("incompatible"):
                continue
            target_id = str(metadata.get("id", "")).strip()
            target_nodes = active_by_id.get(norm(target_id), [])
            if len(target_nodes) > 1:
                add_ambiguous_reference(module, target_id, "DependedModuleMetadata")
                continue
            if target_nodes:
                target = node_to_module[target_nodes[0]]
                pair = tuple(sorted((norm(module.module_id), norm(target.module_id))))
                if pair[0] != pair[1] and norm(module.module_id) < norm(target.module_id):
                    issues.append({
                        "kind": "incompatible_modules", "severity": "Critical" if active_state_known else "Medium",
                        "modules": [module.module_id, target.module_id], "target": target.module_id,
                        "description": (
                            f"Метаданные {module.name} объявляют {target.name} несовместимым модулем; оба включены."
                            if active_state_known else
                            f"Метаданные {module.name} объявляют {target.name} несовместимым модулем. Оба установлены, "
                            "но активность модулей не удалось определить — подтвердите, включены ли оба."
                        ),
                        "source": "DependedModuleMetadata",
                    })

    finalize_version_issues()

    # Check the real order currently saved by the launcher/BEM, when it was found.
    current_pos = {
        node_key: node_to_module[node_key].load_order
        for node_key in node_to_module
        if node_to_module[node_key].load_order is not None
    }
    violations: list[dict[str, Any]] = []
    if order_source:
        for edge, (before_key, after_key) in zip(constraints, constraint_nodes):
            before_pos = current_pos.get(before_key)
            after_pos = current_pos.get(after_key)
            if before_pos is not None and after_pos is not None and before_pos >= after_pos:
                violation = dict(edge)
                violation["current_before_position"] = before_pos + 1
                violation["current_after_position"] = after_pos + 1
                violations.append(violation)
                issues.append({
                    "kind": "order_violation", "severity": "High",
                    "modules": [edge["before_id"], edge["after_id"]],
                    "description": (
                        f"Нарушено правило порядка: {edge['before_id']} должен загружаться раньше "
                        f"{edge['after_id']} ({edge['source']})."
                    ),
                    "source": edge["source"],
                })

    # Stable topological sort. Unique internal node keys keep duplicate folder entries
    # visible instead of silently overwriting one in a Module-ID dictionary.
    adjacency: dict[str, set[str]] = {node_key: set() for node_key in node_to_module}
    indegree: dict[str, int] = {node_key: 0 for node_key in node_to_module}
    for before_key, after_key in constraint_nodes:
        if after_key in adjacency[before_key] or before_key not in adjacency or after_key not in adjacency:
            continue
        adjacency[before_key].add(after_key)
        indegree[after_key] += 1

    canonical_official = {
        "native": 0, "sandboxcore": 1, "birthanddeath": 2,
        "custombattle": 3, "sandbox": 4, "storymode": 5, "navaldlc": 6,
    }

    def priority(node_key: str) -> tuple[Any, ...]:
        module = node_to_module[node_key]
        module_id = norm(module.module_id)
        # The existing launcher/Vortex order is used only to report violations;
        # it must never steer BCI's own recommendation.
        if module.is_official:
            return (0, canonical_official.get(module_id, 50), module.folder.casefold(), node_key)
        return (1, module.folder.casefold(), node_key)

    # Build topological generations. Every module in one generation is currently
    # ready under the known graph, so BCI has no declared rule that fixes their
    # mutual order. A stable tie-breaker keeps the export reproducible without
    # pretending that alphabetical order is a compatibility requirement.
    sorted_ids: list[str] = []
    node_groups: dict[str, int | None] = {}
    group_number = 1
    remaining_indegree = dict(indegree)
    while True:
        ready_nodes = sorted(
            (node for node, degree in remaining_indegree.items() if degree == 0 and node not in node_groups),
            key=priority,
        )
        if not ready_nodes:
            break
        for node in ready_nodes:
            sorted_ids.append(node)
            node_groups[node] = group_number
        for node in ready_nodes:
            for nxt in sorted(adjacency[node]):
                remaining_indegree[nxt] -= 1
        group_number += 1

    # Tarjan SCC identifies actual cyclic components (not downstream nodes merely blocked by a cycle).
    index_counter = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indexes: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    cycle_groups: list[list[str]] = []

    def strongconnect(node: str) -> None:
        nonlocal index_counter
        indexes[node] = index_counter
        lowlinks[node] = index_counter
        index_counter += 1
        stack.append(node)
        on_stack.add(node)
        for nxt in sorted(adjacency[node]):
            if nxt not in indexes:
                strongconnect(nxt)
                lowlinks[node] = min(lowlinks[node], lowlinks[nxt])
            elif nxt in on_stack:
                lowlinks[node] = min(lowlinks[node], indexes[nxt])
        if lowlinks[node] == indexes[node]:
            component = []
            while stack:
                member = stack.pop()
                on_stack.remove(member)
                component.append(member)
                if member == node:
                    break
            if len(component) > 1 or node in adjacency[node]:
                cycle_groups.append(component)

    for node in adjacency:
        if node not in indexes:
            strongconnect(node)
    cycle_ids = {node for group in cycle_groups for node in group}
    blocked_ids = [node for node, degree in remaining_indegree.items() if degree > 0]
    if cycle_groups:
        blocking_reasons.append("В графе есть циклы: все ограничения порядка одновременно выполнить невозможно")
        for group in cycle_groups:
            cycle_names = sorted((node_to_module[node].module_id for node in group), key=str.casefold)
            issues.append({
                "kind": "dependency_cycle", "severity": "Critical" if active_state_known else "High",
                "modules": cycle_names,
                "description": (
                    "Обнаружен цикл правил загрузки между активными модулями: "
                    if active_state_known else
                    "Обнаружен потенциальный цикл правил загрузки среди установленных модулей "
                    "(активность не подтверждена): "
                ) + ", ".join(cycle_names) + ". Одновременно выполнить все ограничения невозможно.",
                "source": "LoadOrderGraph",
            })
        notes.append("Модули в цикле отмечены отдельно; зависимые от цикла модули также не могут быть однозначно размещены.")

    # Keep every module visible even if a cycle prevented full topological sorting.
    for node in sorted((node for node in blocked_ids if node not in sorted_ids), key=priority):
        sorted_ids.append(node)
        node_groups[node] = None

    # Calculate how much of the pairwise order is actually supported by declared
    # metadata. This is evidence coverage, not a probability of compatibility.
    reachability: dict[str, set[str]] = {}
    for start_node in adjacency:
        seen: set[str] = set()
        stack_nodes = list(adjacency[start_node])
        while stack_nodes:
            current_node = stack_nodes.pop()
            if current_node in seen:
                continue
            seen.add(current_node)
            stack_nodes.extend(adjacency[current_node] - seen)
        reachability[start_node] = seen

    total_pairs = len(node_to_module) * (len(node_to_module) - 1) // 2
    determined_pairs = 0
    contradictory_pairs = 0
    per_node_determined: dict[str, int] = {node: 0 for node in node_to_module}
    ordered_node_keys = list(node_to_module)
    for left_index, left_node in enumerate(ordered_node_keys):
        for right_node in ordered_node_keys[left_index + 1:]:
            left_to_right = right_node in reachability[left_node]
            right_to_left = left_node in reachability[right_node]
            if left_to_right != right_to_left:
                determined_pairs += 1
                per_node_determined[left_node] += 1
                per_node_determined[right_node] += 1
            elif left_to_right and right_to_left:
                contradictory_pairs += 1

    pairwise_coverage = round(100 * determined_pairs / total_pairs, 1) if total_pairs else None
    rank_reasons: dict[str, list[str]] = defaultdict(list)
    rank_sources: dict[str, list[dict[str, str]]] = defaultdict(list)
    for edge, (before_key, after_key) in zip(constraints, constraint_nodes):
        rank_reasons[after_key].append(f"после {edge['before_id']}: {edge['reason']}")
        rank_reasons[before_key].append(f"перед {edge['after_id']}: {edge['reason']}")
        source_item = {"source": str(edge.get("source", "")), "strength": str(edge.get("strength", ""))}
        rank_sources[after_key].append(source_item)
        rank_sources[before_key].append(source_item)

    recommended = []
    for index, node_key in enumerate(sorted_ids, 1):
        module = node_to_module[node_key]
        current = current_pos.get(node_key)
        is_ambiguous = norm(module.module_id) in ambiguous_module_ids
        module_sources = rank_sources.get(node_key, [])
        source_strengths = {item["strength"] for item in module_sources if item["strength"]}
        if "Required" in source_strengths:
            placement_basis = "Обязательная зависимость из DependedModules"
        elif "Declared" in source_strengths:
            placement_basis = "Явное правило ModulesToLoadAfterThis"
        elif "Advisory" in source_strengths:
            placement_basis = "Правило порядка из DependedModuleMetadata (BLSE)"
        elif "Optional" in source_strengths:
            placement_basis = "Объявленная необязательная зависимость"
        elif module.is_official:
            placement_basis = "Базовая последовательность официальных модулей BCI"
        else:
            placement_basis = "Нейтральная сортировка по имени папки; обязательный порядок не задан"
        relative_total = max(0, len(node_to_module) - 1)
        relative_known = per_node_determined.get(node_key, 0)
        relative_coverage = round(100 * relative_known / relative_total, 1) if relative_total else None
        recommended.append({
            "position": index,
            "order_group": node_groups.get(node_key),
            "module_id": module.module_id,
            "name": module.name,
            "folder": module.folder,
            "current_position": current + 1 if current is not None else None,
            "is_official": module.is_official,
            "unresolved_cycle": node_key in cycle_ids,
            "blocked_by_cycle": node_key in blocked_ids and node_key not in cycle_ids,
            "ambiguous_identifier": is_ambiguous,
            "placement_basis": placement_basis,
            "relative_order_determined_pairs": relative_known,
            "relative_order_total_pairs": relative_total,
            "relative_order_coverage_pct": relative_coverage,
            "reasons": rank_reasons.get(node_key, [])[:20],
        })

    critical_issues = [issue for issue in issues if issue.get("severity") == "Critical"]
    if ambiguous_module_ids:
        recommendation_status = "blocked_by_duplicate_ids"
    elif cycle_groups:
        recommendation_status = "blocked_by_cycles"
    elif critical_issues:
        recommendation_status = "blocked_by_critical_issues"
    elif not active_state_known or not order_source:
        recommendation_status = "provisional"
    elif any(issue.get("severity") == "Medium" for issue in issues):
        recommendation_status = "ready_with_warnings"
    else:
        recommendation_status = "ready"

    safe_for_manual_use = recommendation_status in {"ready", "ready_with_warnings"}
    if not active_state_known:
        notes.append("Активность всех модулей не подтверждена; порядок предварительный и не должен применяться без проверки профиля.")
    if not order_source:
        notes.append("Текущий порядок не найден. Он не нужен для расчёта собственной рекомендации BCI, но без профиля активный набор модов остаётся неподтверждённым.")
    if ambiguous_module_ids:
        notes.append("Обнаружены повторяющиеся Module ID. Все папки оставлены в списке, но рекомендация заблокирована до устранения неоднозначности.")
    if order_source:
        notes.append(f"Фактический порядок прочитан из: {order_source}. Он используется только для проверки нарушений и отображения текущих позиций; собственная рекомендация BCI рассчитывается независимо от него.")

    return {
        "current_order_known": bool(order_source),
        "current_order_source": order_source,
        "constraints": constraints,
        "violations": violations,
        "issues": issues,
        "cycles": [[node_to_module[x].module_id for x in sorted(group)] for group in cycle_groups],
        "recommended_order": recommended,
        "pairwise_order_evidence": {
            "total_pairs": total_pairs,
            "determined_pairs": determined_pairs,
            "undetermined_pairs": max(0, total_pairs - determined_pairs - contradictory_pairs),
            "contradictory_pairs": contradictory_pairs,
            "coverage_pct": pairwise_coverage,
            "interpretation": (
                "Доля пар модулей, чей взаимный порядок выводится из объявленных правил. "
                "Это не вероятность совместимости, стабильности или отсутствия вылетов."
            ),
        },
        "recommendation_status": recommendation_status,
        "safe_for_manual_use": safe_for_manual_use,
        "blocking_reasons": blocking_reasons + [
            issue.get("description", "") for issue in critical_issues
        ],
        "ambiguous_module_ids": ambiguous_module_ids,
        "notes": notes,
    }

def _build_file_order_interactions(
    findings: list[Finding],
    modules: list[ModuleInfo],
    load_order_analysis: dict[str, Any],
) -> list[dict[str, Any]]:
    """Summarize file-overlap pairs without inventing an ordering direction."""
    module_by_id: dict[str, list[ModuleInfo]] = defaultdict(list)
    for module in modules:
        module_by_id[norm(module.module_id)].append(module)

    adjacency: dict[str, set[str]] = defaultdict(set)
    for edge in load_order_analysis.get("constraints", []):
        before_id = norm(edge.get("before_id"))
        after_id = norm(edge.get("after_id"))
        if before_id and after_id:
            adjacency[before_id].add(after_id)

    def find_path(start: str, target: str) -> list[str] | None:
        if start == target:
            return [start]
        queue_nodes: list[list[str]] = [[start]]
        seen = {start}
        while queue_nodes:
            path = queue_nodes.pop(0)
            for next_id in sorted(adjacency.get(path[-1], set())):
                if next_id == target:
                    return path + [next_id]
                if next_id not in seen:
                    seen.add(next_id)
                    queue_nodes.append(path + [next_id])
        return None

    pairs: dict[tuple[str, str], dict[str, Any]] = {}
    for finding in findings:
        if finding.category not in {"XML Attributes", "Resource Override"}:
            continue
        ids = list(dict.fromkeys(str(value) for value in finding.modules if str(value).strip()))
        if len(ids) != 2:
            continue
        key = tuple(sorted((norm(ids[0]), norm(ids[1]))))
        item = pairs.setdefault(key, {
            "modules": sorted(ids, key=str.casefold),
            "xml_conflict_count": 0,
            "xml_same_value_write_count": 0,
            "resource_different_paths": [],
            "resource_identical_paths": [],
            "evidence_confidence": "High",
            "relationship": None,
            "compatibility_patch_candidates": [],
        })
        evidence = finding.evidence or {}
        if finding.category == "XML Attributes":
            item["xml_conflict_count"] += int(evidence.get("contested_count", 0) or 0)
            item["xml_same_value_write_count"] += int(evidence.get("same_value_writes", evidence.get("count", 0)) or 0)
            if evidence.get("relationship"):
                item["relationship"] = evidence.get("relationship")
            for candidate in evidence.get("compatibility_patch_candidates", []):
                if candidate not in item["compatibility_patch_candidates"]:
                    item["compatibility_patch_candidates"].append(candidate)
            # Finding confidence already accounts for XSD-backed versus heuristic keys.
            # Keep it separate from direction certainty: high-confidence overlap does
            # not tell us which module should win.
            if finding.confidence != "High":
                item["evidence_confidence"] = "Medium"
        else:
            rel = str(evidence.get("relative_path", ""))
            if evidence.get("content_differs") is True:
                item["resource_different_paths"].append(rel)
            elif evidence.get("comparison_complete") is True:
                item["resource_identical_paths"].append(rel)
            if finding.confidence != "High":
                item["evidence_confidence"] = "Medium"

    result: list[dict[str, Any]] = []
    for key, item in pairs.items():
        left, right = key
        left_mods = module_by_id.get(left, [])
        right_mods = module_by_id.get(right, [])
        ambiguous = len(left_mods) != 1 or len(right_mods) != 1
        official_pair = (
            len(left_mods) == 1 and len(right_mods) == 1
            and left_mods[0].is_official and right_mods[0].is_official
        )
        left_to_right = find_path(left, right) if not ambiguous else None
        right_to_left = find_path(right, left) if not ambiguous else None
        if left_to_right and right_to_left:
            direction_status = "conflicting"
            direction_text = "В графе есть противоречивые пути в обе стороны. Порядок нельзя считать разрешённым."
        elif left_to_right:
            direction_status = "determined"
            direction_text = f"Правила задают направление: {' → '.join(left_to_right)}."
        elif right_to_left:
            direction_status = "determined"
            direction_text = f"Правила задают направление: {' → '.join(right_to_left)}."
        else:
            direction_status = "undetermined"
            direction_text = "Файлы пересекаются, но статические данные не определяют, какой мод должен перезаписывать другой."

        has_order_sensitive_evidence = (
            item["xml_conflict_count"] > 0 or bool(item["resource_different_paths"])
        )
        relationship = item.get("relationship") or {}
        if official_pair:
            recommendation = (
                "Оба модуля относятся к официальной игре/DLC. Совпадения могут быть частью штатной композиции игры; "
                "не переставлять их только на основании пересечения файлов."
            )
        elif relationship.get("classification") == "addon_override_candidate":
            recommendation = (
                direction_text + " BCI считает пару кандидатом на штатное переопределение дополнения, "
                "поскольку дополнение объявляет зависимость от базового мода. Это не доказывает правильность "
                "итоговых данных; проверьте целевой игровой сценарий."
            )
        elif direction_status == "determined":
            recommendation = (
                direction_text + " Это соблюдает метаданные, но само по себе не доказывает, что файловое взаимодействие исправлено."
            )
        elif direction_status == "conflicting":
            recommendation = "Не применять автоматическую рекомендацию для этой пары: сначала разрешить противоречие графа."
        elif has_order_sensitive_evidence:
            recommendation = (
                "Не добавлять произвольное правило порядка. Для этой пары требуется контролируемое сравнение двух вариантов "
                "загрузки (A→B и B→A) и проверка целевого игрового сценария."
            )
        else:
            recommendation = "Совпадающие данные сами по себе не дают основания менять взаимный порядок."

        item.update({
            "official_pair": official_pair,
            "ambiguous_module_ids": ambiguous,
            "direction_status": direction_status,
            "direction_evidence": direction_text,
            "relationship": relationship,
            "compatibility_patch_candidates": item.get("compatibility_patch_candidates", []),
            "order_sensitive": has_order_sensitive_evidence,
            "recommendation": recommendation,
        })
        result.append(item)

    result.sort(
        key=lambda row: (
            not row["order_sensitive"],
            -row["xml_conflict_count"],
            -len(row["resource_different_paths"]),
            [value.casefold() for value in row["modules"]],
        )
    )
    return result


def scan_modules(
    game_path: str | Path,
    bem_profile_path: str | Path | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    force_rehash: bool = False,
    store: BCIStore | None = None,
) -> dict[str, Any]:
    """Scan Bannerlord modules and build BCI's independent order recommendation."""
    scan_started = time.perf_counter()
    stage_timings_ms: dict[str, float] = {}
    performance_stats: dict[str, int] = {
        "modules_found": 0,
        "modules_analyzed": 0,
        "files_discovered": 0,
        "fingerprinted_files": 0,
        "fingerprint_cache_hits": 0,
        "fingerprint_cache_misses": 0,
        "fingerprint_cache_written": 0,
        "fingerprint_unreadable_files": 0,
        "bytes_hashed": 0,
        "xml_files_parsed": 0,
        "xsd_files_parsed": 0,
        "xslt_files_parsed": 0,
        "resource_candidates": 0,
        "xml_records_indexed": 0,
        "mod_pairs_considered": 0,
        "mod_pairs_with_shared_xml": 0,
        "shared_xml_objects_compared": 0,
        "official_override_records_checked": 0,
        "resource_collisions_checked": 0,
        "resource_collision_files_hashed": 0,
        "resource_collision_bytes_hashed": 0,
        "resource_collision_size_mismatch_paths": 0,
        "resource_collision_files_skipped_by_size": 0,
    }
    discovery_started = time.perf_counter()
    _emit_progress(progress_callback, "detect", "Определение папки игры", 0, 1, 1, str(game_path))
    game_paths = resolve_game_paths(game_path)
    game_root = game_paths.game_root
    modules_path = game_paths.modules_dir
    _emit_progress(progress_callback, "detect", "Папка игры найдена", 1, 1, 4, str(game_root))

    explicit_bem = parse_bem_profile(Path(bem_profile_path)) if bem_profile_path else {}
    profile_info: dict[str, Any] = {}
    discovery_details: list[dict[str, Any]] = []
    module_dirs = sorted((p for p in modules_path.iterdir() if p.is_dir()), key=lambda p: p.name.casefold())
    _emit_progress(progress_callback, "discovery", "Поиск модулей", 0, len(module_dirs), 6, f"Найдено папок: {len(module_dirs)}")

    modules: list[ModuleInfo] = []
    for index, module_dir in enumerate(module_dirs, 1):
        info = _read_manifest(module_dir, explicit_bem.get(norm(module_dir.name)))
        if info is not None:
            if explicit_bem:
                entry = explicit_bem.get(norm(info.module_id))
                if entry is not None:
                    info.enabled = bool(entry.get("enabled", True))
                    info.enabled_known = bool(entry.get("enabled_known", False))
                    info.load_order = entry.get("index")
                    info.load_order_source = "explicit load-order file"
                    info.is_official = info.is_official or entry.get("is_official", False)
                    info.name = entry.get("name") or info.name
                else:
                    # An omitted module is not proof that the launcher disabled it. Include it
                    # conservatively and report its activation state as unknown.
                    info.enabled = True
                    info.enabled_known = False
                    info.load_order = None
                    info.load_order_source = "incomplete explicit load-order file"
            modules.append(info)
        _emit_progress(progress_callback, "discovery", "Чтение SubModule.xml", index, len(module_dirs), 6 + int(12 * index / max(1, len(module_dirs))), module_dir.name)

    # A manually selected profile JSON is authoritative for the selected file.
    # Otherwise discover Vortex first, then BEM/vanilla Launcher.
    order_source = str(Path(bem_profile_path).expanduser().resolve()) if bem_profile_path else None
    order_source_type = None
    detected_order: dict[str, dict[str, Any]] = explicit_bem
    discovery_notes: list[str] = []
    if explicit_bem:
        order_source_type = next(
            (entry.get("source_type") for entry in explicit_bem.values() if entry.get("source_type")),
            "JSON profile",
        )
        profile_info.update(load_order_file_profile_metadata(Path(bem_profile_path)))
        profile_info["provider"] = order_source_type
        discovery_notes = [
            "Профиль JSON выбран пользователем вручную; автоматический поиск снимков Vortex/BEM пропущен.",
            f"Ручной файл профиля принят: {order_source} ({len(explicit_bem)} записей Module ID).",
        ]
        discovery_details.append({
            "path": str(Path(bem_profile_path).resolve()), "status": "accepted_manual",
            "reason": "Пользователь явно выбрал этот JSON; ручной источник имеет приоритет над автообнаружением.",
            "entries": len(explicit_bem), "created_at": profile_info.get("load_order_created_at"),
        })
        if profile_info.get("metadata_note"):
            discovery_notes.append(str(profile_info["metadata_note"]))
        if profile_info.get("game_mode") in {"singleplayer", "multiplayer"}:
            for entry in explicit_bem.values():
                entry["game_mode"] = profile_info["game_mode"]
        else:
            profile_modes_from_entries = {
                str(entry.get("game_mode")).casefold() for entry in explicit_bem.values()
                if entry.get("game_mode") in {"singleplayer", "multiplayer"}
            }
            if len(profile_modes_from_entries) == 1:
                profile_info["game_mode"] = next(iter(profile_modes_from_entries))
    else:
        detected_order, order_source, discovery_notes = discover_load_order(
            game_root, {norm(m.module_id) for m in modules}, progress_callback=progress_callback,
            profile_info=profile_info, diagnostics=discovery_details,
        )
        if detected_order:
            order_source_type = next(
                (entry.get("source_type") for entry in detected_order.values() if entry.get("source_type")),
                "Launcher",
            )

    profile_entries = explicit_bem or detected_order
    profile_is_complete = bool(profile_info.get("profile_complete")) or any(
        bool(entry.get("profile_complete")) for entry in profile_entries.values()
    )
    if profile_is_complete:
        profile_info["profile_complete"] = True
    profile_modes = {
        str(entry.get("game_mode")).casefold()
        for entry in profile_entries.values()
        if entry.get("game_mode") in {"singleplayer", "multiplayer"}
    }
    profile_game_mode = next(iter(profile_modes)) if len(profile_modes) == 1 else None
    if len(profile_modes) > 1:
        discovery_notes.append("В профиле обнаружены противоречивые значения режима игры; SP/MP-фильтрация отключена.")

    for module in modules:
        entry = profile_entries.get(norm(module.module_id)) if profile_entries else None
        if entry is None:
            if profile_entries and profile_is_complete:
                # A full Vortex/BUTR profile JSON enumerates the modules in that
                # profile. An installed module omitted from this complete list is
                # not part of the selected profile; do not pull it into analysis.
                module.enabled = False
                module.enabled_known = True
                module.load_order = None
                module.load_order_source = "not present in complete Vortex profile"
            else:
                # Partial/legacy launcher sources cannot prove that an omitted
                # module is disabled, so keep the conservative unknown state.
                module.enabled = True
                module.enabled_known = False
                module.load_order = None
                module.load_order_source = "not present in detected profile" if profile_entries else "unknown"
            continue
        module.enabled_known = bool(entry.get("enabled_known", False))
        module.enabled = bool(entry.get("enabled", True))
        module.load_order = entry.get("index")
        module.load_order_source = f"{entry.get('source_type') or 'profile'}: {order_source}"
        module.name = entry.get("name") or module.name
        module.is_official = module.is_official or bool(entry.get("is_official", False))

    scope_modules = _apply_profile_game_mode(modules, profile_game_mode)

    _emit_progress(progress_callback, "profile", "Определение активного профиля", 1, 1, 20,
                   f"Источник: {order_source or 'не найден; проверяются все установленные модули'}")
    stage_timings_ms["discovery_ms"] = round((time.perf_counter() - discovery_started) * 1000, 1)
    performance_stats["modules_found"] = len(modules)

    # Build one file inventory per module and reuse it in all later scan stages.
    inventory_started = time.perf_counter()
    file_inventories = {module.path: _collect_module_files(Path(module.path)) for module in modules}
    performance_stats["files_discovered"] = sum(len(item.all_files) for item in file_inventories.values())
    stage_timings_ms["inventory_ms"] = round((time.perf_counter() - inventory_started) * 1000, 1)

    # Fingerprint all modules, including disabled ones, to detect changes against Baseline.
    fingerprint_started = time.perf_counter()
    hash_cache = store.get_file_hash_cache() if store is not None and not force_rehash else {}
    pending_hashes: dict[str, tuple[int, int, str]] = {}
    for index, module in enumerate(modules, 1):
        _emit_progress(progress_callback, "fingerprint", "Снятие отпечатков файлов", index - 1, len(modules), 20 + int(15 * (index - 1) / max(1, len(modules))), module.name)
        inventory = file_inventories[module.path]
        module.fingerprint = _make_fingerprint(
            module, inventory.all_files, performance_stats,
            hash_cache=hash_cache, pending_hashes=pending_hashes, force_rehash=force_rehash,
        )
        _emit_progress(progress_callback, "fingerprint", "Снятие отпечатков файлов", index, len(modules), 20 + int(15 * index / max(1, len(modules))), module.name)
    if store is not None:
        performance_stats["fingerprint_cache_written"] = store.upsert_file_hash_cache(pending_hashes)
    stage_timings_ms["fingerprint_ms"] = round((time.perf_counter() - fingerprint_started) * 1000, 1)

    _emit_progress(progress_callback, "schemas", "Чтение XSD-схем", 0, 1, 36, "Поиск уникальных ключей XML")
    xsd_started = time.perf_counter()
    performance_stats["xsd_files_parsed"] = sum(len(item.xsd_files) for item in file_inventories.values())
    xsd_keys = _load_xsd_unique_attributes(modules, file_inventories)
    stage_timings_ms["xsd_ms"] = round((time.perf_counter() - xsd_started) * 1000, 1)
    _emit_progress(progress_callback, "schemas", "XSD-схемы обработаны", 1, 1, 40, f"Правил ключей: {len(xsd_keys)}")
    all_records: list[XmlRecord] = []
    xslt_results: dict[str, list[dict[str, Any]]] = {}
    resource_maps: dict[str, dict[str, tuple[str, str]]] = {}
    enabled_modules = [m for m in scope_modules if m.enabled]
    performance_stats["modules_analyzed"] = len(enabled_modules)
    module_analysis_started = time.perf_counter()
    xml_extract_ms = 0.0
    xslt_scan_ms = 0.0
    resource_inventory_ms = 0.0
    for index, module in enumerate(enabled_modules, 1):
        start_percent = 40 + int(34 * (index - 1) / max(1, len(enabled_modules)))
        _emit_progress(progress_callback, "xml", "Анализ XML, XSLT и ресурсов", index - 1, len(enabled_modules), start_percent, module.name)
        inventory = file_inventories[module.path]
        performance_stats["xml_files_parsed"] += len(inventory.xml_files)
        performance_stats["xslt_files_parsed"] += len(inventory.xslt_files)
        performance_stats["resource_candidates"] += len(inventory.resource_files)
        phase_started = time.perf_counter()
        records, errors = _extract_records(module, xsd_keys, inventory.xml_files)
        xml_extract_ms += (time.perf_counter() - phase_started) * 1000
        all_records.extend(records)
        module.parse_errors.extend(errors)
        phase_started = time.perf_counter()
        xslt_results[module.module_id] = _scan_xslt(module, inventory.xslt_files)
        xslt_scan_ms += (time.perf_counter() - phase_started) * 1000
        phase_started = time.perf_counter()
        resource_maps[module.module_id] = _resource_inventory(module, inventory.resource_files)
        resource_inventory_ms += (time.perf_counter() - phase_started) * 1000
        module.resource_files = list(resource_maps[module.module_id].keys())
        _emit_progress(progress_callback, "xml", "Анализ XML, XSLT и ресурсов", index, len(enabled_modules), 40 + int(34 * index / max(1, len(enabled_modules))), module.name)

    stage_timings_ms["module_analysis_ms"] = round((time.perf_counter() - module_analysis_started) * 1000, 1)
    stage_timings_ms["xml_extract_ms"] = round(xml_extract_ms, 1)
    stage_timings_ms["xslt_scan_ms"] = round(xslt_scan_ms, 1)
    stage_timings_ms["resource_inventory_ms"] = round(resource_inventory_ms, 1)
    analysis_started = time.perf_counter()
    findings: list[Finding] = []
    # Unknown modules are included in the static analysis conservatively, but they are not
    # counted as confirmed enabled modules and do not make the active order authoritative.
    active = [m for m in scope_modules if m.enabled]
    enabled_known_count = sum(1 for module in scope_modules if module.enabled_known)
    report_enabled_state_known = bool(scope_modules) and enabled_known_count == len(scope_modules)
    if report_enabled_state_known:
        enabled_state_status = "known"
    elif enabled_known_count:
        enabled_state_status = "partial"
    else:
        enabled_state_status = "unknown"

    # Duplicate IDs remain a finding even when one copy is disabled. Severity is
    # based on the currently confirmed activation state, not folder count alone.
    phase_started = time.perf_counter()
    active_id_counts: dict[str, int] = defaultdict(int)
    for module in active:
        active_id_counts[norm(module.module_id)] += 1
    scoped_id_counts: dict[str, list[str]] = defaultdict(list)
    for module in scope_modules:
        scoped_id_counts[norm(module.module_id)].append(module.folder)
    for module_id, folders in scoped_id_counts.items():
        if len(folders) <= 1:
            continue
        active_count = active_id_counts.get(module_id, 0)
        if active_count > 1 and report_enabled_state_known:
            severity = "Critical"
            description = "Несколько активных папок объявляют один и тот же Module ID. Нельзя однозначно определить целевой модуль для зависимостей и порядка загрузки."
        elif active_count > 1:
            severity = "High"
            description = "Несколько папок с одинаковым Module ID включены в консервативный анализ; активность не подтверждена. Рекомендация порядка заблокирована до уточнения профиля и устранения дубля."
        else:
            severity = "Medium"
            description = "Несколько папок объявляют один и тот же Module ID. Сейчас одновременно активные копии не подтверждены, но установка остаётся неоднозначной."
        findings.append(Finding(
            finding_id=f"duplicate-module-{module_id}", category="Manifest", severity=severity,
            title=f"Повторяющийся Module ID: {module_id}",
            description=description,
            modules=folders, evidence={"module_id": module_id, "folders": folders, "active_copies": active_count},
            risk_points=50 if active_count > 1 else 15, confidence="High",
            recommendation="Проверить дублирующиеся установки и оставить одну корректную копию. До этого не применять автоматическую рекомендацию порядка."
        ))
    stage_timings_ms["duplicate_module_checks_ms"] = round((time.perf_counter() - phase_started) * 1000, 1)

    order_known = bool(order_source and report_enabled_state_known and all(m.load_order is not None for m in scope_modules))
    phase_started = time.perf_counter()
    load_order_analysis = analyze_load_order(scope_modules, order_source if order_known else None)
    stage_timings_ms["load_order_graph_ms"] = round((time.perf_counter() - phase_started) * 1000, 1)
    _emit_progress(progress_callback, "loadorder", "Анализ зависимостей и порядка загрузки", 0, 1, 78, "Построение графа ограничений")
    phase_started = time.perf_counter()
    for issue_index, issue in enumerate(load_order_analysis["issues"], 1):
        category_by_kind = {
            "missing_dependency": "Dependencies",
            "dependency_version_mismatch": "Dependencies",
            "dependency_version_unknown": "Dependencies",
            "dependency_version_vanilla_mismatch": "Dependencies",
            "ambiguous_module_reference": "Dependencies",
            "metadata_version_mismatch": "Load Order",
            "metadata_version_unknown": "Load Order",
            "metadata_version_vanilla_mismatch": "Load Order",
            "order_violation": "Load Order",
            "dependency_cycle": "Load Order",
            "incompatible_modules": "Incompatibility",
        }
        kind = issue.get("kind", "load_order")
        if kind == "missing_dependency":
            finding_id = f"missing-dep-{stable_json_hash(issue.get('modules', []))[:8]}-{norm(issue.get('target', ''))}"
            title = f"Отсутствует обязательная зависимость: {issue.get('target', '')}"
            recommendation = "Установить и включить требуемую зависимость либо отключить модуль, который от неё зависит."
            risk_points = 35
        elif kind == "dependency_version_mismatch":
            finding_id = f"dependency-version-{stable_json_hash(issue.get('modules', []))[:10]}-{stable_json_hash(issue.get('normalized_constraint', ''))[:8]}"
            if issue.get("optional"):
                title = "Необязательное требование к версии отличается"
                recommendation = "Проверить при необходимости; правило объявлено optional и не должно блокировать запуск."
                risk_points = 0
            else:
                title = "Не совпадает версия обязательной зависимости"
                recommendation = "Сверить версии мода и зависимости с требованиями автора. Это предупреждение о расхождении версий, а не доказательство вылета."
                risk_points = 10
        elif kind == "dependency_version_unknown":
            finding_id = f"dependency-version-unknown-{stable_json_hash(issue.get('modules', []))[:10]}-{issue_index}"
            title = "Не удалось проверить версию зависимости"
            recommendation = "Проверьте SubModule.xml установленной зависимости: в нём должна быть указана версия."
            risk_points = 0
        elif kind == "dependency_version_vanilla_mismatch":
            finding_id = f"dependency-version-info-{stable_json_hash(issue.get('modules', []))[:10]}-{issue_index}"
            title = "Версия зависимости соответствует требованиям BUTR"
            recommendation = "Действий по версии не требуется. При запуске через Vanilla Launcher отдельно обратите внимание на его возможное предупреждение о формате/версии."
            risk_points = 0
        elif kind == "metadata_version_mismatch":
            finding_id = f"metadata-version-{stable_json_hash(issue.get('modules', []))[:10]}-{stable_json_hash(issue.get('normalized_constraint', ''))[:8]}"
            if issue.get("optional"):
                title = "Необязательное правило BLSE по версии отличается"
                recommendation = "Проверить при необходимости; метаданные помечены optional и не должны блокировать запуск."
                risk_points = 0
            else:
                title = "Версия в правиле порядка BLSE отличается"
                recommendation = "Проверьте, актуально ли правило DependedModuleMetadata для установленной версии целевого мода или игры."
                risk_points = 5
        elif kind == "metadata_version_unknown":
            finding_id = f"metadata-version-unknown-{stable_json_hash(issue.get('modules', []))[:10]}-{issue_index}"
            title = "Не удалось проверить версию в правиле BLSE"
            recommendation = "Проверьте версию целевого модуля и условие в DependedModuleMetadata."
            risk_points = 0
        elif kind == "metadata_version_vanilla_mismatch":
            finding_id = f"metadata-version-info-{stable_json_hash(issue.get('modules', []))[:10]}-{issue_index}"
            title = "Версия в правиле BLSE соответствует требованиям BUTR"
            recommendation = "Действий по версии не требуется. Требование минимальной версии/диапазона выполнено."
            risk_points = 0
        elif kind == "ambiguous_module_reference":
            finding_id = f"ambiguous-module-ref-{stable_json_hash(issue.get('modules', []))[:10]}-{issue_index}"
            title = f"Неоднозначная ссылка на модуль: {issue.get('target', '')}"
            recommendation = "Устранить дублирующиеся Module ID. BCI намеренно не выбирает одну папку наугад."
            risk_points = 25
        elif kind == "order_violation":
            finding_id = f"order-violation-{stable_json_hash(issue.get('modules', []))[:12]}-{issue_index}"
            title = "Нарушено объявленное правило порядка загрузки"
            recommendation = "Проверить рекомендацию BCI и при подтверждённом профиле исправить порядок в BEM/Launcher. BCI не меняет его автоматически."
            risk_points = 20
        elif kind == "dependency_cycle":
            finding_id = f"load-cycle-{stable_json_hash(issue.get('modules', []))[:12]}"
            title = "Циклические требования порядка загрузки"
            recommendation = "Проверить манифесты модов и compatibility patches: одновременно выполнить все ограничения невозможно."
            risk_points = 40
        else:
            finding_id = f"incompatible-{stable_json_hash(issue.get('modules', []))[:12]}-{issue_index}"
            title = "Два модуля объявлены несовместимыми"
            recommendation = "Проверить авторские условия совместимости. Если оба мода нужны, найти специальный compatibility patch."
            risk_points = 45
        findings.append(Finding(
            finding_id=finding_id,
            category=category_by_kind.get(kind, "Load Order"),
            severity=issue.get("severity", "Medium"),
            title=title,
            description=issue.get("description", "Обнаружена проблема в правилах модулей."),
            modules=issue.get("modules", []),
            evidence=issue,
            risk_points=risk_points,
            confidence=(
                "High" if kind in {"missing_dependency", "dependency_version_mismatch"} and not issue.get("optional") or
                (kind in {"dependency_cycle", "incompatible_modules", "ambiguous_module_reference"} and report_enabled_state_known)
                else "Medium"
            ),
            recommendation=recommendation,
        ))
    _emit_progress(progress_callback, "loadorder", "Граф загрузки построен", 1, 1, 83,
                   f"Ограничений: {len(load_order_analysis['constraints'])}; нарушений: {len(load_order_analysis['violations'])}")
    stage_timings_ms["load_order_findings_ms"] = round((time.perf_counter() - phase_started) * 1000, 1)

    # XML object index and official baselines.
    phase_started = time.perf_counter()
    records_by_module: dict[str, dict[tuple[str, str, str], XmlRecord]] = defaultdict(dict)
    for record in all_records:
        key = (norm(record.table), norm(record.tag), record.key)
        # Last record in a module wins in our inventory; keep one for pair analysis.
        records_by_module[record.module_id][key] = record
    performance_stats["xml_records_indexed"] = len(all_records)
    stage_timings_ms["xml_record_index_ms"] = round((time.perf_counter() - phase_started) * 1000, 1)

    phase_started = time.perf_counter()
    official = [m for m in active if m.is_official]
    official_ids = {m.module_id for m in official}
    official_order = {m.module_id: (m.load_order if m.load_order is not None else i) for i, m in enumerate(official)}
    baseline_records: dict[tuple[str, str, str], dict[str, str]] = {}
    official_records = sorted((r for r in all_records if r.module_id in official_ids), key=lambda r: official_order.get(r.module_id, 0))
    for record in official_records:
        key = (norm(record.table), norm(record.tag), record.key)
        # Approximation of official module composition order; exact merge is schema-dependent.
        baseline_records.setdefault(key, {}).update(record.attributes)
    stage_timings_ms["official_xml_baseline_ms"] = round((time.perf_counter() - phase_started) * 1000, 1)

    modded = [m for m in active if not m.is_official]
    # Informational inventory of mods that override official data. Intentional overrides
    # are not counted as conflicts unless another mod competes for the same property.
    official_overrides: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    phase_started = time.perf_counter()
    for record in all_records:
        if record.module_id in official_ids:
            continue
        performance_stats["official_override_records_checked"] += 1
        obj_key = (norm(record.table), norm(record.tag), record.key)
        base_attrs = baseline_records.get(obj_key, {})
        changed = [{"attribute": attr, "official_value": base_attrs[attr], "mod_value": value}
                   for attr, value in record.attributes.items() if attr in base_attrs and base_attrs[attr] != value]
        if changed:
            official_overrides[(record.module_id, record.table)].append({
                "tag": record.tag, "key": record.key, "source": record.source, "changes": changed[:100]
            })
    for (module_id, table), overrides in official_overrides.items():
        findings.append(Finding(
            finding_id=f"official-override-{norm(module_id)}-{stable_json_hash(table)[:8]}",
            category="Official Data Override", severity="Info",
            title=f"{module_id} изменяет официальные данные: {table}",
            description="Модуль меняет значения, отличающиеся от официальной базы игры. Это обычный механизм модификаций и само по себе не является конфликтом. Конкурирующие изменения других модов анализируются отдельно.",
            modules=[module_id], evidence={"table": table, "objects_changed": len(overrides), "examples": overrides[:100]},
            risk_points=0, confidence="High",
            recommendation="Действий не требуется только на основании этого факта; проверить ожидаемое итоговое поведение и наличие других модов, меняющих те же атрибуты."
        ))
    stage_timings_ms["official_override_scan_ms"] = round((time.perf_counter() - phase_started) * 1000, 1)

    phase_started = time.perf_counter()
    pair_acc = _compare_mod_xml_records(
        modded, records_by_module, baseline_records, performance_stats,
        load_order_constraints=load_order_analysis.get("constraints", []),
    )
    # Preserve the historic finding order (module-list pair order) while the
    # index itself is traversed by XML object. Only pairs with shared XML keys
    # are sorted here; the full Cartesian product is no longer materialized.
    module_positions: dict[str, int] = {}
    for index, module in enumerate(modded):
        module_positions.setdefault(module.module_id, index)
    ordered_pair_acc = sorted(
        pair_acc.items(),
        key=lambda item: tuple(sorted((
            module_positions.get(item[0][0], len(modded)),
            module_positions.get(item[0][1], len(modded)),
        ))),
    )
    for (mod_a_id, mod_b_id), acc in ordered_pair_acc:
        # Capped interaction risk per pair; repeated contested writes increase
        # the score but cannot dominate alone.
        acc["risk"] = min(45, len(acc["contested"]) * 5)
        if not acc["contested"] and not acc["same_write"]:
            continue
        severity = "High" if len(acc["contested"]) >= 5 else ("Medium" if acc["contested"] else "Info")
        if acc["contested"]:
            relationship = acc.get("relationship") or {}
            ordered_candidate = relationship.get("classification") in {
                "addon_override_candidate", "declared_ordered_overlap"
            }
            classification = relationship.get("classification", "independent_competing_edits")
            if ordered_candidate:
                severity = "Info"
                risk_points = 0
                if classification == "addon_override_candidate":
                    title = f"XML-переопределение дополнения: {mod_a_id} ↔ {mod_b_id}"
                    description = (
                        relationship.get("description", "") + " BCI классифицирует пару как кандидата на штатное "
                        "переопределение дополнения, а не как независимый конфликт. Это не доказывает, что итоговые "
                        "значения правильны или что игровой сценарий полностью совместим."
                    )
                    recommendation = (
                        f"Сохранить объявленное направление {relationship.get('before_id')} → "
                        f"{relationship.get('after_id')} и проверить итоговые XML-данные и целевой игровой сценарий. "
                        "Не отключать мод только на основании различий атрибутов."
                    )
                else:
                    title = f"XML-пересечение с объявленным порядком: {mod_a_id} ↔ {mod_b_id}"
                    description = (
                        relationship.get("description", "") + " Отличающиеся значения остаются файловым "
                        "пересечением; заданное направление само по себе не доказывает корректность результата."
                    )
                    recommendation = "Соблюдать явно объявленное направление и проверить фактическое поведение игры."
            else:
                risk_points = acc["risk"]
                title = f"Конкурирующие XML-изменения: {mod_a_id} ↔ {mod_b_id}"
                description = (
                    "Независимые модули задают разные значения одного и того же атрибута XML-объекта. "
                    "Это подтверждает конкурирующую перезапись, но само по себе ещё не доказывает краш "
                    "или некорректное поведение."
                )
                recommendation = (
                    "Проверить итоговое значение после объединения XML и выполнить целевой игровой сценарий. "
                    "При необходимости найти подтверждённый compatibility patch."
                )
            findings.append(Finding(
                finding_id=f"xml-contest-{norm(mod_a_id)}-{norm(mod_b_id)}", category="XML Attributes",
                severity=severity,
                title=title,
                description=description,
                modules=[mod_a_id, mod_b_id], evidence={
                    "classification": classification,
                    "relationship": relationship,
                    "compatibility_patch_candidates": acc.get("compatibility_patch_candidates", []),
                    "contested_attributes": acc["contested"][:500],
                    "contested_count": len(acc["contested"]),
                    "same_value_writes": len(acc["same_write"]),
                },
                risk_points=risk_points, confidence="High" if all(x["key_confidence"] == "XSD" for x in acc["contested"]) else "Medium",
                recommendation=recommendation,
            ))
        elif acc["same_write"]:
            findings.append(Finding(
                finding_id=f"xml-aligned-{norm(mod_a_id)}-{norm(mod_b_id)}", category="XML Attributes", severity="Info",
                title=f"Совпадающие XML-изменения: {mod_a_id} ↔ {mod_b_id}",
                description="Оба мода устанавливают одинаковые значения атрибутов. Это не считается конфликтом по данным атрибутам.",
                modules=[mod_a_id, mod_b_id], evidence={"same_value_writes": acc["same_write"][:100], "count": len(acc["same_write"])},
                risk_points=0, confidence="Medium", recommendation="Действий не требуется, если итоговые данные соответствуют ожидаемому поведению."
            ))
    stage_timings_ms["xml_pair_comparison_ms"] = round((time.perf_counter() - phase_started) * 1000, 1)

    # Parse failures.
    phase_started = time.perf_counter()
    for module in active:
        for error in module.parse_errors:
            findings.append(Finding(
                finding_id=f"xml-parse-{norm(module.module_id)}-{stable_json_hash(error)[:8]}", category="XML Parse", severity="High",
                title=f"Ошибка разбора XML в {module.name}", description=error,
                modules=[module.module_id], evidence={"error": error}, risk_points=25, confidence="High",
                recommendation="Исправить XML или проверить кодировку/структуру файла."
            ))
    stage_timings_ms["xml_parse_error_findings_ms"] = round((time.perf_counter() - phase_started) * 1000, 1)

    # Resource path collisions: compare sizes first. Different sizes prove that
    # resources are not byte-identical, so reading both large files for SHA-256
    # would add I/O without changing the finding. Equal-sized files still require
    # full hashes to distinguish identical content from same-size modifications.
    phase_started = time.perf_counter()
    resource_hash_elapsed_ms = 0.0
    resource_owners: dict[str, list[tuple[ModuleInfo, str]]] = defaultdict(list)
    for module in active:
        module_resources = resource_maps.get(module.module_id, {})
        for rel in module.resource_files:
            # Keep the original path casing for case-sensitive filesystems.
            relative = module_resources.get(rel, ("", rel))[1]
            resource_owners[rel].append((module, relative))
    for rel, owners in resource_owners.items():
        owner_ids = sorted({m.module_id for m, _ in owners}, key=str.casefold)
        if len(owner_ids) < 2:
            continue
        performance_stats["resource_collisions_checked"] += 1
        owner_evidence: list[dict[str, Any]] = []
        owner_paths: list[tuple[Path, dict[str, Any]]] = []
        for mod, relative in owners:
            resource_path = Path(mod.path) / relative
            try:
                size_bytes = resource_path.stat().st_size
                item = {"module": mod.module_id, "size_bytes": size_bytes, "sha256": None}
                owner_evidence.append(item)
                owner_paths.append((resource_path, item))
            except OSError as exc:
                item = {"module": mod.module_id, "error": str(exc), "sha256": None}
                owner_evidence.append(item)

        known_sizes = {item["size_bytes"] for _, item in owner_paths}
        size_mismatch = len(known_sizes) > 1
        all_owners_stat_ok = len(owner_paths) == len(owners)
        if size_mismatch:
            # File length is sufficient proof that at least two resources differ.
            # Avoid opening and hashing potentially very large assets.
            performance_stats["resource_collision_size_mismatch_paths"] += 1
            performance_stats["resource_collision_files_skipped_by_size"] += len(owner_paths)
            different = True
            comparison_method = "size"
            comparison_complete = all_owners_stat_ok
        elif not all_owners_stat_ok:
            # Do not report identical content when one or more file sizes could not be inspected.
            different = False
            comparison_method = "incomplete"
            comparison_complete = False
        else:
            for resource_path, item in owner_paths:
                hash_started = time.perf_counter()
                try:
                    digest = sha256_file(resource_path)
                    item["sha256"] = digest
                    performance_stats["resource_collision_files_hashed"] += 1
                    performance_stats["resource_collision_bytes_hashed"] += int(item["size_bytes"])
                except OSError as exc:
                    item["error"] = str(exc)
                finally:
                    resource_hash_elapsed_ms += (time.perf_counter() - hash_started) * 1000
            distinct_hashes = {item["sha256"] for item in owner_evidence if item.get("sha256")}
            comparison_complete = all(item.get("sha256") for item in owner_evidence)
            different = len(distinct_hashes) > 1
            comparison_method = "sha256" if comparison_complete else "incomplete"

        if different is True:
            description = (
                "Несколько модулей содержат разные файлы по одному относительному пути. "
                "Это подтверждённая конкурирующая замена ресурса, но не доказанный краш."
            )
            severity = "Low"
            confidence = "High" if comparison_complete else "Medium"
        elif not comparison_complete:
            description = (
                "Не удалось полностью сравнить содержимое всех файлов с одинаковым относительным "
                "путём. Совпадение содержимого не подтверждено."
            )
            severity = "Info"
            confidence = "Low"
        else:
            description = (
                "Несколько модулей содержат идентичный ресурс по одному относительному пути; "
                "конфликт содержимого не обнаружен."
            )
            severity = "Info"
            confidence = "High"

        findings.append(Finding(
            finding_id=f"resource-overlap-{stable_json_hash(rel)[:12]}", category="Resource Override", severity=severity,
            title=f"Совпадает путь ресурса: {rel}",
            description=description,
            modules=owner_ids,
            evidence={
                "relative_path": rel,
                "owners": owner_evidence,
                "content_differs": different,
                "comparison_method": comparison_method,
                "comparison_complete": comparison_complete,
            },
            risk_points=2 if different else 0,
            confidence=confidence,
            recommendation="Проверить, является ли замена намеренной и какой ресурс фактически загружается движком."
        ))
    stage_timings_ms["resource_collision_hash_ms"] = round(resource_hash_elapsed_ms, 1)
    stage_timings_ms["resource_collision_analysis_ms"] = round((time.perf_counter() - phase_started) * 1000, 1)

    # XSLT presence is reported separately; without running the game merge pipeline it is not safe to claim exact effects.
    phase_started = time.perf_counter()
    for module in active:
        for xslt in xslt_results.get(module.module_id, []):
            if "error" in xslt:
                findings.append(Finding(
                    finding_id=f"xslt-parse-{norm(module.module_id)}-{stable_json_hash(xslt['path'])[:8]}", category="XSLT", severity="Medium",
                    title=f"Не удалось разобрать XSLT: {module.name}", description=xslt["error"], modules=[module.module_id],
                    evidence=xslt, risk_points=8, confidence="High", recommendation="Проверить корректность XSLT."
                ))
            else:
                findings.append(Finding(
                    finding_id=f"xslt-review-{norm(module.module_id)}-{stable_json_hash(xslt['path'])[:8]}", category="XSLT", severity="Info",
                    title=f"Обнаружено XSLT-преобразование: {module.name}",
                    description="Наличие XSLT само по себе не является конфликтом. Текущая версия BCI извлекает операции и XPath, но пока не исполняет XSLT в точности как игровой движок.",
                    modules=[module.module_id], evidence=xslt, risk_points=0, confidence="High",
                    recommendation="Для глубокой проверки сопоставить XPath с затрагиваемыми XML-таблицами и подтвердить результат в игровом загрузчике."
                ))
    stage_timings_ms["xslt_findings_ms"] = round((time.perf_counter() - phase_started) * 1000, 1)

    # File overlaps inform pairwise investigation, but never invent a winner when
    # no declared rule determines which module should load later.
    file_interaction_started = time.perf_counter()
    load_order_analysis["file_interactions"] = _build_file_order_interactions(
        findings, modules, load_order_analysis
    )
    unresolved_file_pairs = [
        item for item in load_order_analysis["file_interactions"]
        if item.get("order_sensitive")
        and item.get("direction_status") == "undetermined"
        and not item.get("official_pair")
        and not item.get("ambiguous_module_ids")
    ]
    load_order_analysis["manual_use_warnings"] = [
        f"Для пары {' ↔ '.join(item.get('modules', []))} обнаружены пересекающиеся файлы, "
        "но направление желаемого переопределения не установлено. Проверьте оба порядка в игре."
        for item in unresolved_file_pairs
    ]
    if unresolved_file_pairs and load_order_analysis.get("recommendation_status") == "ready":
        load_order_analysis["recommendation_status"] = "ready_with_warnings"
    if unresolved_file_pairs:
        load_order_analysis["notes"].append(
            f"Есть {len(unresolved_file_pairs)} сторонних файловых пар с неустановленным направлением переопределения. "
            "Рекомендация соблюдает известные правила, но эти пары требуют отдельной проверки."
        )
    stage_timings_ms["load_order_file_interactions_ms"] = round(
        (time.perf_counter() - file_interaction_started) * 1000, 1
    )

    # Rank third-party mods only. Official game modules remain visible in the
    # inventory/findings, but must never appear as a recommended removal target.
    phase_started = time.perf_counter()
    ranking_modules = [m for m in active if not m.is_official]
    ranking_ids = {m.module_id for m in ranking_modules}
    scores: dict[str, dict[str, Any]] = {
        m.module_id: {"risk_index": 0, "findings": [], "peer_mods": set()}
        for m in ranking_modules
    }
    for finding in findings:
        if finding.risk_points <= 0:
            continue
        implicated_mods = [mid for mid in finding.modules if mid in ranking_ids]
        if not implicated_mods:
            continue
        # Attribute the finding to third-party modules only. This prevents Native,
        # Sandbox or DLC modules from rising to the top of a list of removal targets.
        share = max(1, round(finding.risk_points / max(1, len(implicated_mods))))
        for module_id in implicated_mods:
            scores[module_id]["risk_index"] += share
            scores[module_id]["findings"].append(finding.finding_id)
            scores[module_id]["peer_mods"].update(x for x in implicated_mods if x != module_id)
    ranking = []
    for module in ranking_modules:
        score = min(100, scores[module.module_id]["risk_index"])
        ranking.append({
            "module_id": module.module_id,
            "name": module.name,
            "folder": module.folder,
            "risk_index": score,
            "finding_count": len(scores[module.module_id]["findings"]),
            "related_mods": sorted(scores[module.module_id]["peer_mods"], key=str.casefold),
            "evidence_confidence": "High" if any(f.confidence == "High" and module.module_id in f.modules for f in findings) else "Medium",
            "is_official": module.is_official,
            "load_order": module.load_order,
        })
    ranking.sort(key=lambda x: (x["risk_index"], x["finding_count"]), reverse=True)
    stage_timings_ms["ranking_ms"] = round((time.perf_counter() - phase_started) * 1000, 1)

    phase_started = time.perf_counter()
    modules_out = []
    for m in modules:
        modules_out.append(asdict(m))

    _emit_progress(progress_callback, "ranking", "Формирование рейтинга конфликтности", 0, 1, 91, "Атрибуция находок к модам")
    ranked_findings = [asdict(f) for f in sorted(findings, key=lambda x: (x.risk_points, x.severity), reverse=True)]
    _emit_progress(progress_callback, "report", "Формирование отчёта", 1, 1, 97, "Подготовка HTML/JSON и карты порядка")
    enabled_state_known = report_enabled_state_known
    confirmed_enabled_count = sum(1 for module in scope_modules if module.enabled_known and module.enabled)
    modules_in_analysis = len(active)
    stage_timings_ms["report_data_assembly_ms"] = round((time.perf_counter() - phase_started) * 1000, 1)
    stage_timings_ms["comparison_and_report_ms"] = round((time.perf_counter() - analysis_started) * 1000, 1)
    performance = {
        "scan_total_ms": round((time.perf_counter() - scan_started) * 1000, 1),
        "stages_ms": stage_timings_ms,
        "stats": performance_stats,
    }
    return {
        "app": APP_NAME,
        "app_version": APP_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "game_root_path": str(game_root),
        "game_modules_path": str(modules_path),
        "game_executable": str(game_paths.executable) if game_paths.executable else None,
        "bem_profile": str(Path(bem_profile_path).resolve()) if bem_profile_path else None,
        "launcher_order_source": order_source,
        "launcher_order_source_type": order_source_type,
        "profile_game_mode": profile_game_mode,
        "profile_info": profile_info,
        "launcher_discovery_details": discovery_details,
        "profile_scope_modules": len(scope_modules),
        "force_rehash": bool(force_rehash),
        "load_order_known": order_known,
        "enabled_state_known": enabled_state_known,
        "enabled_state_status": enabled_state_status,
        "enabled_known_modules": enabled_known_count,
        "enabled_unknown_modules": len(scope_modules) - enabled_known_count,
        "analysis_scope": (
            ("Профиль Vortex Singleplayer; сетевые модули исключены, если профиль явно не включил их. " if profile_game_mode == "singleplayer" else
             "Профиль Vortex Multiplayer. " if profile_game_mode == "multiplayer" else "")
            + ("Состав профиля подтверждён свежим состоянием Vortex; состояние модулей в области анализа известно"
               if profile_info.get("active_profile_verified") and enabled_state_status == "known"
               else "Состав профиля предположен по самому свежему файлу порядка Vortex; проверьте профиль в Vortex. Состояние модулей в области анализа известно"
               if profile_info.get("selection_method") == "latest_recent_profile_file" and enabled_state_status == "known"
               else "Выбранный JSON профиля полный; состав модулей в нём принят как источник истины"
               if profile_is_complete and enabled_state_status == "known"
               else "Состояние всех модулей в области анализа подтверждено"
               if enabled_state_status == "known"
               else "Часть состояния неизвестна; неизвестные модули включены в анализ"
               if enabled_state_status == "partial"
               else "Активный профиль не найден; анализируются все установленные модули")
        ),
        "launcher_discovery_notes": discovery_notes,
        "load_order_analysis": load_order_analysis,
        "performance": performance,
        "summary": {
            "modules_found": len(modules),
            "modules_enabled": modules_in_analysis,
            "modules_in_analysis": modules_in_analysis,
            "profile_scope_modules": len(scope_modules),
            "modules_confirmed_enabled": confirmed_enabled_count,
            "modules_enabled_known": enabled_state_known,
            "modules_enabled_known_count": enabled_known_count,
            "modules_enabled_unknown_count": len(scope_modules) - enabled_known_count,
            "official_modules": sum(1 for m in active if m.enabled_known and m.enabled and m.is_official),
            "findings_total": len(findings),
            "critical": sum(1 for f in findings if f.severity == "Critical"),
            "high": sum(1 for f in findings if f.severity == "High"),
            "medium": sum(1 for f in findings if f.severity == "Medium"),
            "info": sum(1 for f in findings if f.severity == "Info"),
            "xml_records": len(all_records),
            "xsd_unique_rules": len(xsd_keys),
            "load_order_constraints": len(load_order_analysis.get("constraints", [])),
            "load_order_violations": len(load_order_analysis.get("violations", [])),
            "load_order_cycles": len(load_order_analysis.get("cycles", [])),
            "load_order_pairwise_coverage_pct": load_order_analysis.get("pairwise_order_evidence", {}).get("coverage_pct"),
            "load_order_file_sensitive_pairs": sum(1 for item in load_order_analysis.get("file_interactions", []) if item.get("order_sensitive")),
        },
        "modules": modules_out,
        "ranking": ranking,
        "findings": ranked_findings,
        "limitations": [
            "Risk Index — экспертный индекс приоритета расследования, не вероятность краша и не доказательство виновности.",
            r"Если профиль Vortex/BEM/Launcher отсутствует или неполон, состояние не подтверждённых модулей остаётся неизвестным; такие модули консервативно включаются в анализ. Для Vortex BCI сначала использует свежий снимок полного состояния и точный профильный JSON из %APPDATA%\Vortex\mountandblade2bannerlord\mods; без снимка допускается только однозначный самый свежий профильный JSON (не старше 30 минут), который явно помечается как предположение. При неоднозначности выберите JSON вручную.",
            "Собственная сортировка BCI строится по зависимостям и явным правилам SubModule.xml/BLSE; порядок текущего лаунчера используется только для аудита нарушений. Между модулями без направленных правил позиция остаётся нейтральной.",
            "Пересечения XML-объектов и одинаковых путей ресурсов показывают чувствительность к порядку, но сами по себе не определяют, какой мод должен загружаться позже. Для этого нужны проверенные правила или A/B игровые тесты.",
            "Для одиночной игры фактический порядок передаётся загрузчиком из списка launcher; зависимости являются правилами/ограничениями лаунчера, а не автоматическим runtime-сортировщиком движка.",
            "XML keys используют XSD unique/key, где схема найдена; иначе применяется эвристика id/name.",
            "Текущий MVP не воспроизводит полностью внутренний XML merge engine Bannerlord.",
            "XSLT пока извлекается и классифицируется, но не исполняется как игровой pipeline.",
            "DLL/IL, Harmony, runtime exceptions, save compatibility и War Sails deep checks входят в следующие этапы.",
            "Перезапись ресурса или XML-атрибута не считается доказанным крашем без воспроизводимого теста.",
        ],
    }


def baseline_directory(modules_dir: str | Path) -> Path:
    resolved = str(Path(modules_dir).expanduser().resolve())
    key = hashlib.sha256(resolved.casefold().encode("utf-8")).hexdigest()[:16]
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    else:
        root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return root / "BCI" / "profiles" / key / "baseline.json"


def make_baseline(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "modules_path": report["game_modules_path"],
        "game_context": {"bem_profile": report.get("bem_profile"), "load_order_known": report.get("load_order_known")},
        "modules": {
            m["module_id"]: {
                "folder": m["folder"], "name": m["name"], "version": m["version"],
                "fingerprint": m.get("fingerprint", ""), "enabled": m["enabled"],
                "is_official": m["is_official"], "load_order": m.get("load_order")
            }
            for m in report["modules"]
        },
    }


def save_baseline(report: dict[str, Any]) -> Path:
    path = baseline_directory(report["game_modules_path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(make_baseline(report), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def compare_with_baseline(report: dict[str, Any]) -> dict[str, Any]:
    """Compare current module inventory with the last user-approved baseline.

    A module is marked CHANGED if its content fingerprint, declared version,
    enabled state, or load-order index changed. This is a change detector, not
    proof that the module caused a regression.
    """
    path = baseline_directory(report["game_modules_path"])
    if not path.exists():
        for row in report.get("ranking", []):
            row["baseline_status"] = "No baseline"
            row["new_candidate"] = False
            row["candidate_impact"] = None
        return {
            "available": False, "new_modules": [], "changed_modules": [],
            "removed_modules": [], "message": "Baseline ещё не сохранён."
        }
    try:
        baseline = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        for row in report.get("ranking", []):
            row["baseline_status"] = "Baseline unreadable"
            row["new_candidate"] = False
            row["candidate_impact"] = None
        return {"available": False, "error": str(exc), "new_modules": [], "changed_modules": [], "removed_modules": []}

    old = baseline.get("modules", {})
    current = {m["module_id"]: m for m in report["modules"]}
    new_modules = [m for mid, m in current.items() if mid not in old]
    changed_modules = []
    change_reasons: dict[str, list[str]] = {}
    for mid, module in current.items():
        if mid not in old:
            continue
        previous = old[mid]
        reasons = []
        if module.get("fingerprint") != previous.get("fingerprint"):
            reasons.append("content fingerprint")
        if module.get("version") != previous.get("version"):
            reasons.append("module version")
        if module.get("enabled") != previous.get("enabled"):
            reasons.append("enabled state")
        if module.get("load_order") != previous.get("load_order"):
            reasons.append("load order")
        if reasons:
            changed_modules.append(module)
            change_reasons[mid] = reasons
    removed_modules = [old[mid] for mid in old if mid not in current]

    new_ids = {m["module_id"] for m in new_modules}
    changed_ids = {m["module_id"] for m in changed_modules}
    for row in report.get("ranking", []):
        mid = row["module_id"]
        if mid in new_ids:
            row["baseline_status"] = "NEW"
            row["new_candidate"] = True
        elif mid in changed_ids:
            row["baseline_status"] = "CHANGED"
            row["new_candidate"] = True
        elif mid in old:
            row["baseline_status"] = "BASELINE"
            row["new_candidate"] = False
        else:
            row["baseline_status"] = "UNKNOWN"
            row["new_candidate"] = False
        row["change_reasons"] = change_reasons.get(mid, [])
        row["candidate_impact"] = 0 if row["new_candidate"] else None

    # Candidate impact is an investigation-priority contribution, not culpability.
    # When a finding links a new/changed candidate with baseline modules, attribute
    # the full finding weight to the candidate(s) for triage, while preserving the
    # symmetric module risk scores calculated by the scanner.
    findings = report.get("findings", [])
    for row in report.get("ranking", []):
        if not row.get("new_candidate"):
            continue
        candidate_id = row["module_id"]
        score = 0.0
        for finding in findings:
            if candidate_id not in finding.get("modules", []) or finding.get("risk_points", 0) <= 0:
                continue
            candidates_in_finding = [
                mid for mid in finding.get("modules", []) if mid in new_ids or mid in changed_ids
            ]
            divisor = max(1, len(candidates_in_finding))
            score += float(finding["risk_points"]) / divisor
        row["candidate_impact"] = min(100, round(score))

    report["ranking"].sort(
        key=lambda x: (
            bool(x.get("new_candidate")),
            x.get("candidate_impact") or 0,
            x["risk_index"],
            x["finding_count"],
        ),
        reverse=True,
    )
    return {
        "available": True,
        "path": str(path),
        "new_modules": new_modules,
        "changed_modules": changed_modules,
        "removed_modules": removed_modules,
        "change_reasons": change_reasons,
        "candidate_count": len(new_ids | changed_ids),
    }


def format_load_order_export(report: dict[str, Any]) -> str:
    """Format BCI's own recommendation for safe manual reference in a launcher."""
    order = report.get("load_order_analysis", {})
    rows = order.get("recommended_order", [])
    status = order.get("recommendation_status", "provisional")
    status_labels = {
        "ready": "Готово: обязательные проверки пройдены",
        "ready_with_warnings": "Готово с предупреждениями: изучите замечания",
        "provisional": "Предварительно: активный набор модов подтверждён не полностью",
        "blocked_by_duplicate_ids": "ЗАБЛОКИРОВАНО: повторяющиеся Module ID",
        "blocked_by_cycles": "ЗАБЛОКИРОВАНО: циклы правил загрузки",
        "blocked_by_critical_issues": "ЗАБЛОКИРОВАНО: критические зависимости/несовместимости",
    }
    evidence = order.get("pairwise_order_evidence", {})
    coverage = evidence.get("coverage_pct")
    coverage_text = f"{coverage:.1f}%" if isinstance(coverage, (int, float)) else "не рассчитывается"
    current_order_text = format_current_order_status(order)
    lines = [
        "BCI — СОБСТВЕННАЯ РЕКОМЕНДАЦИЯ ПОРЯДКА ЗАГРУЗКИ",
        f"Версия BCI: {report.get('app_version', APP_VERSION)}",
        f"Создано: {report.get('generated_at', '')}",
        f"Путь игры: {report.get('game_root_path', '')}",
        f"Статус рекомендации: {status_labels.get(status, status)}",
        f"Нарушений текущего порядка: {current_order_text}",
        "",
        "ВАЖНО: это текстовый список для ручной сверки/перестановки в Vortex, не файл нативного импорта Vortex.",
        "BCI не меняла настройки Vortex и не записывала порядок в профиль.",
        f"Покрытие взаимного порядка метаданными: {evidence.get('determined_pairs', 0)} из {evidence.get('total_pairs', 0)} пар ({coverage_text}).",
        "Это доля пар с выводимым из правил взаимным порядком, а не вероятность совместимости или отсутствия вылетов.",
        "",
    ]
    if order.get("manual_use_warnings"):
        lines.append("ФАЙЛОВЫЕ ПАРЫ, ТРЕБУЮЩИЕ РУЧНОЙ ПРОВЕРКИ")
        lines.extend(f"- {warning}" for warning in order.get("manual_use_warnings", []))
        lines.append("")
    if not order.get("safe_for_manual_use"):
        lines.extend([
            "*** ПРЕДУПРЕЖДЕНИЕ: ПРЕДВАРИТЕЛЬНЫЙ/ЗАБЛОКИРОВАННЫЙ РЕЗУЛЬТАТ — НЕ ПРИМЕНЯТЬ КАК ГОТОВУЮ СБОРКУ. ***",
            "Причины:",
        ])
        lines.extend(f"- {reason}" for reason in order.get("blocking_reasons", []))
        lines.extend(["Проверьте активный набор модов, устраните дубли/циклы/критические проблемы и повторите анализ.", ""])

    group_rows: dict[int | None, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        group_rows[row.get("order_group")].append(row)
    for group in sorted((x for x in group_rows if x is not None)):
        lines.append(f"ГРУППА {group} — модули без установленного взаимного приоритета внутри группы")
        for row in sorted(group_rows[group], key=lambda item: item.get("position", 0)):
            current = row.get("current_position")
            current_text = f"; сейчас #{current}" if current is not None else "; текущая позиция неизвестна"
            lines.append(
                f"{row.get('position', 0):03d}. {row.get('module_id', '')} | {row.get('name', '')} "
                f"| папка: {row.get('folder', '')}{current_text}"
            )
            lines.append(f"     Основание: {row.get('placement_basis', 'не определено')}")
            reasons = row.get("reasons", [])
            if reasons:
                lines.append("     Правила: " + "; ".join(reasons))
        lines.append("")

    if None in group_rows:
        lines.append("НЕРАЗРЕШЁННАЯ ОБЛАСТЬ ГРАФА (циклы/зависимые узлы)")
        for row in sorted(group_rows[None], key=lambda item: item.get("position", 0)):
            lines.append(f"{row.get('position', 0):03d}. {row.get('module_id', '')} | {row.get('name', '')} | НЕ РАЗМЕЩЁН ДОСТОВЕРНО")
        lines.append("")

    if order.get("issues"):
        lines.append("ПРЕДУПРЕЖДЕНИЯ АНАЛИЗА")
        for issue in order.get("issues", []):
            lines.append(f"- [{issue.get('severity', 'Info')}] {issue.get('description', '')}")
        lines.append("")

    interactions = order.get("file_interactions", [])
    if interactions:
        lines.append("ФАЙЛОВЫЕ ПЕРЕСЕЧЕНИЯ, ВАЖНЫЕ ДЛЯ ПОРЯДКА")
        for item in interactions:
            xml_count = int(item.get("xml_conflict_count", 0))
            resource_count = len(item.get("resource_different_paths", []))
            if not item.get("order_sensitive"):
                continue
            lines.append(
                f"- {' ↔ '.join(item.get('modules', []))}: XML-атрибутов с разными значениями — {xml_count}; "
                f"разных файлов по одинаковым путям — {resource_count}."
            )
            lines.append(f"  Качество свидетельств пересечения: {item.get('evidence_confidence', 'не оценено')}")
            lines.append(f"  Направление: {item.get('direction_evidence', '')}")
            lines.append(f"  Вывод BCI: {item.get('recommendation', '')}")
        lines.append("")

    lines.extend([
        "ПРАВИЛО ИНТЕРПРЕТАЦИИ",
        "Порядок внутри одной группы может отличаться: BCI не нашла объявленного правила, которое фиксирует взаимное положение этих модулей.",
        "Пересечение XML/ресурсов означает чувствительность результата к порядку, но статический анализ не всегда может определить желаемый вариант.",
        "Проценты описывают покрытие доказательствами порядка, а не вероятность успешного запуска.",
    ])
    return "\n".join(lines) + "\n"


def format_current_order_status(order_analysis: dict[str, Any]) -> str:
    """Format one of the three truthful states for current-order validation."""
    if not order_analysis.get("current_order_known", False):
        return "Не проверено (профиль порядка не подтверждён)"
    return str(len(order_analysis.get("violations", [])))


def export_load_order(report: dict[str, Any], output_path: str | Path) -> Path:
    path = Path(output_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(format_load_order_export(report), encoding="utf-8-sig")
    return path


def export_report(report: dict[str, Any], output_dir: str | Path) -> tuple[Path, Path]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = out / f"BCI_Report_{stamp}.json"
    html_path = out / f"BCI_Report_{stamp}.html"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    rows = []
    for r in report.get("ranking", []):
        impact = r.get("candidate_impact")
        rows.append(
            "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                html.escape(r.get("baseline_status", "")), html.escape(r["name"]), html.escape(r["module_id"]),
                r["risk_index"], impact if impact is not None else "—", r["finding_count"],
                html.escape(", ".join(r["related_mods"]))
            )
        )
    findings_html = []
    for f in report.get("findings", []):
        findings_html.append(
            "<details><summary><b>[{}] {}</b> — {} ({})</summary><p>{}</p><pre>{}</pre></details>".format(
                html.escape(f["severity"]), html.escape(f["title"]), html.escape(", ".join(f["modules"])), f["risk_points"],
                html.escape(f["description"]), html.escape(json.dumps(f.get("evidence", {}), ensure_ascii=False, indent=2))
            )
        )
    summary = report["summary"]
    performance = report.get("performance", {})
    performance_stages = performance.get("stages_ms", {})
    performance_stats = performance.get("stats", {})
    stage_labels = {
        "discovery_ms": "Поиск игры, модулей и профиля",
        "inventory_ms": "Единый обход файлов модулей",
        "fingerprint_ms": "Снятие отпечатков SHA-256",
        "xsd_ms": "Чтение XSD-схем",
        "module_analysis_ms": "Обработка файлов модулей — всего",
        "xml_extract_ms": "  • Разбор XML и построение записей",
        "xslt_scan_ms": "  • Разбор XSLT",
        "resource_inventory_ms": "  • Инвентаризация ресурсов",
        "duplicate_module_checks_ms": "  • Проверка повторяющихся Module ID",
        "load_order_graph_ms": "  • Построение графа порядка загрузки",
        "load_order_findings_ms": "  • Формирование предупреждений о порядке",
        "load_order_file_interactions_ms": "  • Анализ пар с файловыми пересечениями для порядка",
        "xml_record_index_ms": "  • Индексация XML-записей",
        "official_xml_baseline_ms": "  • Построение официальной XML-базы",
        "official_override_scan_ms": "  • Сравнение модов с официальными данными",
        "xml_pair_comparison_ms": "  • Сравнение пар модов по XML",
        "xml_parse_error_findings_ms": "  • Формирование сообщений об ошибках XML",
        "resource_collision_analysis_ms": "  • Проверка совпадающих путей ресурсов",
        "resource_collision_hash_ms": "    • SHA-256 ресурсов одинакового размера",
        "xslt_findings_ms": "  • Формирование сообщений XSLT",
        "ranking_ms": "  • Расчёт рейтинга модов",
        "report_data_assembly_ms": "  • Подготовка данных отчёта",
        "comparison_and_report_ms": "Итого: сравнение и формирование результатов",
    }
    performance_rows = "".join(
        "<tr><td>{}</td><td>{:.1f} с</td></tr>".format(
            html.escape(label), float(performance_stages.get(key, 0)) / 1000
        )
        for key, label in stage_labels.items()
    )
    performance_section = ""
    if performance:
        performance_section = (
            "<h2>Производительность сканирования</h2>"
            "<p><b>Время ядра анализатора:</b> {:.2f} с</p>".format(
                float(performance.get("scan_total_ms", 0)) / 1000
            )
            + "<table><thead><tr><th>Этап</th><th>Время</th></tr></thead><tbody>"
            + performance_rows + "</tbody></table>"
            + "<p class='muted'>Строки со знаком • раскрывают время соответствующего общего этапа и уже входят в него; не суммируйте их повторно. Время ядра не включает Baseline, ML, SQLite и экспорт отчёта.</p>"
            + "<p><b>Файлов обнаружено:</b> {} · <b>Хешировано файлов:</b> {} · "
              "<b>Данных захешировано:</b> {:.1f} МБ · <b>XML-файлов разобрано:</b> {} · "
              "<b>XSLT-файлов разобрано:</b> {} · <b>Кандидатов ресурсов:</b> {} · "
              "<b>XML-записей проиндексировано:</b> {} · <b>Записей сторонних модов проверено:</b> {} · "
              "<b>Возможных пар сторонних модов:</b> {} · <b>Пар с общими XML-объектами:</b> {} · "
              "<b>Сравнений XML-объектов между парами:</b> {} · "
              "<b>Совпадающих путей ресурсов проверено:</b> {} · "
              "<b>Ресурсных файлов захешировано:</b> {} · "
              "<b>SHA-256 из кеша:</b> {} · <b>Новых/изменённых хешей:</b> {} · "
              "<b>Записей кеша обновлено:</b> {} · <b>Полная перепроверка:</b> {} · "
              "<b>Объём хеширования ресурсов:</b> {:.1f} МБ · "
              "<b>Путей с разным размером:</b> {} · "
              "<b>Файлов пропущено по размеру:</b> {}</p>".format(
                  int(performance_stats.get("files_discovered", 0)),
                  int(performance_stats.get("fingerprinted_files", 0)),
                  int(performance_stats.get("bytes_hashed", 0)) / (1024 * 1024),
                  int(performance_stats.get("xml_files_parsed", 0)),
                  int(performance_stats.get("xslt_files_parsed", 0)),
                  int(performance_stats.get("resource_candidates", 0)),
                  int(performance_stats.get("xml_records_indexed", 0)),
                  int(performance_stats.get("official_override_records_checked", 0)),
                  int(performance_stats.get("mod_pairs_considered", 0)),
                  int(performance_stats.get("mod_pairs_with_shared_xml", 0)),
                  int(performance_stats.get("shared_xml_objects_compared", 0)),
                  int(performance_stats.get("resource_collisions_checked", 0)),
                  int(performance_stats.get("resource_collision_files_hashed", 0)),
                  int(performance_stats.get("fingerprint_cache_hits", 0)),
                  int(performance_stats.get("fingerprint_cache_misses", 0)),
                  int(performance_stats.get("fingerprint_cache_written", 0)),
                  "Да" if report.get("force_rehash") else "Нет",
                  int(performance_stats.get("resource_collision_bytes_hashed", 0)) / (1024 * 1024),
                  int(performance_stats.get("resource_collision_size_mismatch_paths", 0)),
                  int(performance_stats.get("resource_collision_files_skipped_by_size", 0)),
              )
        )
    profile_info = report.get("profile_info", {}) or {}
    discovery_details = report.get("launcher_discovery_details", []) or []
    discovery_notes = report.get("launcher_discovery_notes", []) or []
    profile_field_rows = []
    profile_labels = (
        ("Источник", "provider"), ("Способ выбора", "selection_method"),
        ("Имя профиля", "profile_name"), ("ID профиля", "profile_id"),
        ("Режим игры", "game_mode"), ("Файл порядка", "load_order_path"),
        ("mtime файла порядка (UTC)", "load_order_created_at"),
        ("Снимок состояния Vortex", "snapshot_path"),
        ("mtime файла снимка Vortex (UTC; не гарантирует время создания)", "snapshot_created_at"),
        ("Возраст снимка, сек.", "snapshot_age_seconds"),
    )
    for label, key in profile_labels:
        value = profile_info.get(key)
        if value not in (None, ""):
            profile_field_rows.append(
                "<tr><td>{}</td><td>{}</td></tr>".format(html.escape(label), html.escape(str(value)))
            )
    diagnostic_rows_html = "".join(
        "<tr><td>{}</td><td>{}</td><td>{}</td></tr>".format(
            html.escape(str(item.get("path", ""))),
            html.escape(str(item.get("status", "unknown"))),
            html.escape(str(item.get("reason", ""))),
        )
        for item in discovery_details
    )
    discovery_notes_html = "".join(
        "<li>{}</li>".format(html.escape(str(note))) for note in discovery_notes
    )
    profile_diagnostics_section = (
        "<h2>Профиль и диагностика обнаружения</h2>"
        + ("<table><tbody>" + "".join(profile_field_rows) + "</tbody></table>" if profile_field_rows else
           "<p class='warn'>Подтверждённый профиль не определён. Ручной выбор JSON рекомендуется.</p>")
        + "<h3>Проверенные пути и кандидаты</h3>"
        + ("<table><thead><tr><th>Путь</th><th>Результат</th><th>Причина</th></tr></thead><tbody>" + diagnostic_rows_html + "</tbody></table>"
           if diagnostic_rows_html else "<p class='muted'>Диагностические пути отсутствуют.</p>")
        + ("<h3>Дополнительные замечания</h3><ul>" + discovery_notes_html + "</ul>" if discovery_notes_html else "")
    )
    current_order_text = format_current_order_status(report.get("load_order_analysis", {}))
    page = f"""<!doctype html><html lang='ru'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>BCI Report</title>
<style>body{{font-family:Segoe UI,Arial,sans-serif;background:#10151d;color:#e8edf4;margin:32px;line-height:1.45}}h1,h2{{color:#f6c94a}}.muted{{color:#aab6c5}}table{{border-collapse:collapse;width:100%;margin:16px 0}}td,th{{border-bottom:1px solid #354150;padding:9px;text-align:left}}th{{color:#f6c94a}}details{{background:#19222e;padding:12px;margin:8px 0;border-radius:8px}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#0c1118;padding:12px;border-radius:6px}}.warn{{color:#f1bd56}}</style></head><body>
<h1>Bannerlord Compatibility Intelligence</h1><p class='muted'>BCI {html.escape(report['app_version'])} · {html.escape(report['generated_at'])}</p>
<p><b>Модули:</b> {summary.get('modules_confirmed_enabled', 0)} подтверждённо включено · {summary.get('modules_in_analysis', summary['modules_enabled'])} включено в анализ из {summary['modules_found']} найденных · <b>Находки:</b> {summary['findings_total']} · <b>XML-записи:</b> {summary['xml_records']}</p>
<p class='warn'>Состояние профиля: {html.escape(report.get('analysis_scope', 'не определено'))}. Неизвестные по активности модули включаются в анализ консервативно.</p>
{profile_diagnostics_section}
<p class='warn'>Risk Index — приоритет расследования, не вероятность краша и не доказательство виновности. Candidate Impact — приоритет проверки нового/изменённого модуля относительно Baseline.</p>
<h2>Рейтинг модов</h2><table><thead><tr><th>Baseline</th><th>Мод</th><th>Module ID</th><th>Risk Index</th><th>Candidate Impact</th><th>Findings</th><th>Связанные модули</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
<h2>Собственная рекомендация порядка BCI</h2>
<p><b>Статус:</b> {html.escape(report.get('load_order_analysis', {}).get('recommendation_status', 'provisional'))} · <b>Ограничений:</b> {len(report.get('load_order_analysis', {}).get('constraints', []))} · <b>Нарушений текущего порядка:</b> {html.escape(current_order_text)}</p>
<p class='warn'>Покрытие взаимного порядка — доля пар, чей порядок подтверждён объявленными правилами. Это не вероятность совместимости или отсутствия вылетов.</p>
{''.join('<p class="warn">'+html.escape(warning)+'</p>' for warning in report.get('load_order_analysis', {}).get('manual_use_warnings', []))}
<table><thead><tr><th>Позиция</th><th>Группа</th><th>Текущая</th><th>Модуль</th><th>Module ID</th><th>Покрытие пар</th><th>Основание</th><th>Правила</th></tr></thead><tbody>{''.join(
    '<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>'.format(
        row.get('position', ''), row.get('order_group') if row.get('order_group') is not None else 'не разрешена',
        row.get('current_position') if row.get('current_position') is not None else 'неизвестно',
        html.escape(row.get('name', '')), html.escape(row.get('module_id', '')),
        (str(row.get('relative_order_coverage_pct')) + '%') if row.get('relative_order_coverage_pct') is not None else '—',
        html.escape(row.get('placement_basis', '')), html.escape('; '.join(row.get('reasons', [])))
    ) for row in report.get('load_order_analysis', {}).get('recommended_order', [])
)}</tbody></table>
<p><b>Покрытие пар:</b> {report.get('load_order_analysis', {}).get('pairwise_order_evidence', {}).get('determined_pairs', 0)} / {report.get('load_order_analysis', {}).get('pairwise_order_evidence', {}).get('total_pairs', 0)} ({report.get('load_order_analysis', {}).get('pairwise_order_evidence', {}).get('coverage_pct') if report.get('load_order_analysis', {}).get('pairwise_order_evidence', {}).get('coverage_pct') is not None else '—'}%).</p>
<h3>Файловые взаимодействия, чувствительные к порядку</h3>
<table><thead><tr><th>Моды</th><th>XML-поля</th><th>Разные ресурсы</th><th>Качество свидетельств</th><th>Направление</th><th>Вывод BCI</th></tr></thead><tbody>{''.join(
    '<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>'.format(
        html.escape(' ↔ '.join(item.get('modules', []))), item.get('xml_conflict_count', 0),
        len(item.get('resource_different_paths', [])), html.escape(item.get('evidence_confidence', 'не оценено')),
        html.escape(item.get('direction_evidence', '')), html.escape(item.get('recommendation', ''))
    ) for item in report.get('load_order_analysis', {}).get('file_interactions', []) if item.get('order_sensitive')
)}</tbody></table>
<p class='muted'>Модули одной группы не имеют установленного взаимного приоритета внутри этой группы. Список для ручного переноса можно экспортировать из приложения; он не изменяет настройки Vortex.</p>
<h2>Найденные взаимодействия</h2>{''.join(findings_html)}
{performance_section}
<h2>Ограничения текущего MVP</h2><ul>{''.join('<li>'+html.escape(x)+'</li>' for x in report.get('limitations', []))}</ul>
<p class='muted'>Локальный отчёт. Файлы модов не изменялись и не отправлялись в сеть.</p></body></html>"""
    html_path.write_text(page, encoding="utf-8")
    return json_path, html_path
