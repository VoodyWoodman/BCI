# Bannerlord Compatibility Intelligence (BCI)

**BCI 0.3.0-alpha — local-first Bannerlord mod compatibility analyzer**

This repository contains the Windows x64 MVP builder and a GitHub Actions workflow that produces a single `BCI_Setup_0.3.0-alpha_x64.exe` installer.

## Build the installer

1. Open **Actions → Build BCI Windows installer**.
2. Choose **Run workflow** on branch `release/0.3.0-alpha`.
3. Download the `BCI-Windows-Installer` artifact when the workflow succeeds.

The workflow reconstructs and SHA-256 verifies the source builder archive before building it on `windows-latest`. It runs the Python test suite, packages the desktop application and LightGBM runtime, downloads Microsoft's Visual C++ Redistributable at build time, then compiles the one-file installer with Inno Setup 6.

## Source and documentation

The versioned source archive is stored as Base64 parts under `builder-payload/` and is reconstructed by the workflow. The archive contains the complete project under `BCI_MVP/`.

- [Russian MVP guide](BCI_MVP/README_RU.md) (available after the workflow unpacks the archive; also included in the source ZIP)
- [Changelog](BCI_MVP/CHANGELOG_RU.md)
- [Roadmap](BCI_MVP/ROADMAP_RU.md)
- [QA report](BCI_MVP/QA_REPORT_RU.md)

## Important MVP limits

A clean installation includes the analyzer, local SQLite report store, LightGBM runtime, local training pipeline, and model import/export. It does **not** include a trained classifier based on real Bannerlord gameplay data. The first usable model is trained locally after collecting confirmed test outcomes. ML inference is shadow-only and does not change the deterministic Risk Index or assign blame to an individual mod.

The repository is currently on the `release/0.3.0-alpha` branch; the initial `main` branch is only the repository bootstrap.
