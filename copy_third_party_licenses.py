from __future__ import annotations

import re
import shutil
import sys
from importlib import metadata
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: copy_third_party_licenses.py <output-directory>")
        return 2
    output = Path(sys.argv[1])
    output.mkdir(parents=True, exist_ok=True)
    rows: list[str] = []
    copied = 0
    bundled_packages = {
        "lightgbm", "numpy", "scipy", "pyinstaller", "altgraph", "pefile",
        "pywin32-ctypes", "packaging", "pyinstaller-hooks-contrib",
    }
    for dist in sorted(metadata.distributions(), key=lambda item: (item.metadata.get("Name") or "").casefold()):
        name = dist.metadata.get("Name") or "Unknown package"
        if name.casefold() not in bundled_packages:
            continue
        version = dist.version
        license_name = (dist.metadata.get("License") or "See included license file").strip().replace("\n", " ")
        rows.append(f"{name} {version} — {license_name}")
        for item in dist.files or []:
            relative = str(item).replace("\\", "/")
            if not re.search(r"(^|/)(LICENSE|LICENCE|COPYING|NOTICE)(\.[^/]*)?$", relative, re.IGNORECASE):
                continue
            source = Path(dist.locate_file(item))
            try:
                if not source.is_file() or source.stat().st_size > 2_000_000:
                    continue
                safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
                suffix = Path(relative).name
                destination = output / f"{safe_name}-{version}-{suffix}"
                if destination.exists():
                    continue
                shutil.copy2(source, destination)
                copied += 1
            except OSError:
                continue

    notice = [
        "BCI — third-party component notices",
        "",
        "BCI bundles open-source Python libraries and their runtime dependencies.",
        "Where available, package license files are copied into this directory.",
        "",
        *rows,
        "",
        "Microsoft Visual C++ Redistributable is downloaded from Microsoft's official aka.ms endpoint during build and bundled separately.",
        "Redistribution must comply with the applicable Microsoft Visual Studio / Visual C++ Redistributable license terms.",
    ]
    (output / "THIRD_PARTY_NOTICES.txt").write_text("\n".join(notice), encoding="utf-8")
    print(f"Collected {copied} license files from {len(rows)} installed distributions into {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
