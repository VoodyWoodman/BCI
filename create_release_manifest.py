from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


REPOSITORY = "VoodyWoodman/BCI"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_manifest(version: str, installer: Path, portable: Path, output_dir: Path) -> dict:
    import re
    if not re.fullmatch(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?", version):
        raise ValueError("Version must be a SemVer-compatible BCI release version.")
    if not installer.is_file() or not portable.is_file():
        raise FileNotFoundError("Installer and portable ZIP must exist before creating release metadata.")
    if installer.name != f"BCI_Setup_{version}_x64.exe":
        raise ValueError("Installer filename must match BCI_Setup_<version>_x64.exe.")
    if portable.name != f"BCI_Portable_{version}.zip":
        raise ValueError("Portable filename must match BCI_Portable_<version>.zip.")

    installer_info = {
        "name": installer.name,
        "sha256": sha256_file(installer),
        "size_bytes": installer.stat().st_size,
    }
    portable_info = {
        "name": portable.name,
        "sha256": sha256_file(portable),
        "size_bytes": portable.stat().st_size,
    }
    channel = "preview" if "-" in version else "stable"
    manifest = {
        "schema_version": 1,
        "product": "Bannerlord Compatibility Intelligence",
        "repository": REPOSITORY,
        "version": version,
        "channel": channel,
        "architecture": "x64",
        "minimum_updater_version": "0.5.2-alpha",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "assets": {
            "installer": installer_info,
            "portable": portable_info,
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "release-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output_dir / "SHA256SUMS.txt").write_text(
        f"{installer_info['sha256']}  {installer.name}\n{portable_info['sha256']}  {portable.name}\n",
        encoding="utf-8",
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Create BCI GitHub Release manifest and SHA-256 checksums.")
    parser.add_argument("--version", required=True)
    parser.add_argument("--installer", required=True, type=Path)
    parser.add_argument("--portable", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    manifest = create_manifest(args.version, args.installer, args.portable, args.output_dir)
    print(f"Created release manifest for BCI {manifest['version']} ({manifest['channel']}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
