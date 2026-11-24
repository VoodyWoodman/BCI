# Bannerlord Compatibility Intelligence (BCI)

**A local-first analyzer for Mount & Blade II: Bannerlord mod setups.**

[English](README.md) · [Русская версия](README_RU.md)  
[Development roadmap](ROADMAP.md) · [Дорожная карта](ROADMAP_RU.md)

BCI inventories Bannerlord modules and statically analyzes selected interactions between their files and data. Its purpose is to help players investigate compatibility risks using traceable evidence—not to label every overlap as a conflict or promise a crash-free game.

## Current status — 0.3.1-alpha

BCI remains an alpha Windows prototype. This checkpoint consolidates real-installation QA and two targeted analyzer improvements:
- an indexed approach to XML comparisons, comparing only modules that share an XML object;
- a resource-size prefilter: files with different sizes are known to differ, while equal-sized files still require SHA-256.

The source-builder package `BCI_MVP_0.3.1-alpha_Source.zip` is attached separately with this project handoff. **It has not yet been published as a GitHub binary asset.** It is a builder/source archive, not a signed stable installer. Installation, update, and uninstall on a clean machine still require release validation.

## Latest real-installation QA — 24 November 2026

Three consecutive runs were performed on one Windows 11 Bannerlord installation:

| Metric | Result |
|---|---:|
| Active / discovered modules | 12 / 12 |
| Findings | 21 in every report |
| XML records | 185,664 |
| Files discovered | 14,230 |
| Files hashed during inventory | 5,350 (253.7 MB) |
| Analyzer core (`scan_modules`) | 7.51 / 7.58 / 7.58 s |
| Mean analyzer-core time | 7.56 s |

The resource check examined 16 duplicate relative paths: 14 paths had different file sizes (28 files safely skipped SHA-256 because unequal sizes prove unequal byte content); four equal-sized files were hashed (about 0.3 MB). Equal-sized files are still compared by SHA-256.

**Measurement boundary:** the `scan_modules` timer excludes Baseline comparison, ML inference, SQLite persistence, and HTML/JSON export. The 7.56-second mean is not an end-to-end timing. These results represent one 12-module setup, not a performance guarantee for 50–100 mods.

The three reports contained no real third-party XML comparison workload: zero third-party XML records and zero shared-object comparisons. The XML-index improvement is covered by synthetic/differential tests, but has not yet been exercised against real third-party XML overlaps in this user's setup.

## Performance policy

The recent performance work is now a checkpoint, not an ongoing race for lower timings. Further optimization is paused. BCI must not sacrifice analyzer coverage, correctness, evidence quality, or regression protection for speed. Resume performance work only when real larger mod collections show an unacceptable delay and profiling identifies a concrete bottleneck. Any future optimization must preserve findings and pass regression tests.

## What the alpha currently includes

- Bannerlord module discovery and metadata/load-order checks;
- XML attribute-level analysis and use of XSD keys where available;
- XSLT inventory (the analyzer does not execute the complete in-game XSLT pipeline);
- resource-path collision checks with size prefiltering and SHA-256 for equal-sized files;
- HTML/JSON reports with evidence and performance counters;
- local SQLite report storage;
- manual test-outcome labels and an experimental local LightGBM workflow in shadow mode.

The ML workflow does not identify a guilty mod or provide a validated probability of failure. No pre-trained classifier based on a representative real Bannerlord gameplay dataset is included.

## Known limitations

- The analyzer does not reproduce the full Bannerlord XML merge engine.
- XSLT is inventoried but not executed as the game's complete transformation pipeline.
- DLL/IL analysis, Harmony patch interactions, runtime exceptions, crash dumps, save compatibility, and deep War Sails checks are not yet implemented.
- A resource override or XML overlap is not proof of a crash.
- Real 50- and 100-mod collections have not yet been validated.
- Clean-machine installation/upgrade/uninstall release checks are still pending.

## Next priority

The next phase focuses on analyzer quality: validate BEM/Launcher active-module detection, inspect real findings, test real third-party XML overlaps, measure false positives and missed cases, and expand reproducible regression examples. Performance work remains paused unless real workloads show a problem.

See the [roadmap](ROADMAP.md) for planned stages. Version milestones are planning targets, not delivery-date promises.

---

*BCI is an independent community project and is not affiliated with or endorsed by TaleWorlds Entertainment.*
