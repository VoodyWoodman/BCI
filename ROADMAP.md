# BCI Development Roadmap

[English](ROADMAP.md) · [Русская версия](ROADMAP_RU.md) · [Project overview](README.md)

This roadmap describes the intended evolution of Bannerlord Compatibility Intelligence (BCI). Milestones are directional and may change as evidence, tests, and real user needs become clearer.

## Current checkpoint — 0.3.1-alpha: scan and performance baseline

**Status: completed for the currently tested 12-module Windows 11 setup; broader scale validation remains open.**

The current analyzer completed three consecutive runs with 12 active / 12 discovered modules, 185,664 XML records, and the same 21 findings in all reports. Analyzer-core (`scan_modules`) timings were 7.51, 7.58, and 7.58 seconds. The timer excludes Baseline, ML inference, SQLite persistence, and HTML/JSON export.

The resource prefilter was validated on this installation: 16 duplicate relative paths were examined; 14 paths had differing sizes, and four files with equal sizes were SHA-256 checked. The size prefilter is a correctness-preserving shortcut: unequal sizes prove unequal byte content; equal sizes still require hashing.

**Performance policy:** further optimization is paused. Do not trade analyzer coverage or correctness for timing improvements. Resume only when real larger mod setups demonstrate unacceptable delays and profiling identifies a specific bottleneck. Every optimization must preserve findings and pass regression tests.

**Not yet validated:** actual 50- and 100-mod collections, and real third-party XML overlaps. The three current reports recorded zero third-party XML records and zero shared-object XML comparisons.

## Next stage — 0.4.0-alpha: analyzer quality and evidence

**Status: next priority**

- validate game root, BEM/Launcher profile detection, and the enabled state of modules;
- inspect findings from real user setups and classify false positives, harmless overrides, and missed cases;
- test XML comparison against real third-party intersections, not only synthetic fixtures;
- strengthen reproducible golden cases and regression coverage;
- improve evidence links, source locations, error handling, and clear unknown/incomplete states;
- verify stable report contents across repeated runs.

**Exit criteria:** supported checks have reproducible examples; findings can be traced to evidence; known limitations and unknown states are explicit; no correctness regression is accepted for performance gains.

## 0.5.0-alpha — Compatibility evidence and test data

Build a reliable foundation for learning from real compatibility tests. Store game/DLC versions, mod identifiers and hashes, load order, configuration, test scenario, analyzer findings, in-game outcome, and reproduction steps where available. Use controlled A/B/A tests and distinguish confirmed, potential, not reproduced, and unknown outcomes. Synthetic fixtures must remain separate from real gameplay outcomes.

**Exit criteria:** test cases are reproducible, versioned, deduplicated, and traceable to the tested mod setup.

## 0.6.0-alpha — Deeper static analysis

Expand the analyzer incrementally:

- semantic XML references, values, and data relationships;
- XSLT transformation semantics;
- resource and asset dependency analysis;
- DLL inspection (types, methods, references, IL) against the matching game API;
- Harmony patch targets, patch types, and ordering where it can be established.

An overlap, shared method, or patch target is not a conflict without evidence of an incompatible effect.

**Exit criteria:** each analyzer family has fixtures, documented limitations, regression tests, and validation on real mod examples.

## 0.7.0-alpha — Validated machine learning

Introduce ML only after a sufficiently representative set of reviewed real outcomes exists. Keep synthetic fixtures separate, validate on unseen mods/pairs/versions with group-based splits, measure precision, recall, false positives, false negatives, and calibration, and begin in shadow mode. No model score should be presented as a calibrated probability before calibration is demonstrated.

**Exit criteria:** published validation results show whether ML adds value beyond deterministic analysis.

## 0.8.0-beta — Interaction and runtime diagnostics

- analyze interactions between pairs and groups of mods;
- collect relevant game logs, exceptions, and crash data where available;
- link mods, files, patches, runtime events, and controlled test outcomes in an evidence graph;
- rank investigation leads without confusing correlation with cause.

**Exit criteria:** interaction findings are traceable to evidence and reproducible scenarios, with uncertainty clearly communicated.

## 0.9.0 — Release candidate and distribution

- produce and verify a Windows x64 installer on a clean system;
- test installation, launch, upgrade, and uninstall;
- validate backup, recovery, and rollback of local data and application updates;
- complete user documentation and known-limitations notes;
- run regression tests on supported Bannerlord configurations.

**Exit criteria:** repeatable installer builds, successful clean-machine tests, no unresolved release-blocking defects, and complete release notes.

## 1.0.0 — First stable release

BCI 1.0 means that documented core functionality is reliable and its limitations are understood. It does not guarantee that every mod combination is compatible or that crashes are impossible.

## Beyond 1.0 — Long-term directions

### BCI Compatibility Forge

Generate a separate compatibility module (for example, `BCI_Compatibility`) with reviewed patches, adapters, configuration, and a manifest. Original mods remain untouched; users review trade-offs, test, and roll back changes. Respect mod licenses and distribution restrictions.

### Optional local AI assistant

A user-installed local model may help explain complex evidence, summarize logs, or draft potential fixes. It remains optional and cannot replace deterministic checks, evidence, or user approval.

### Reusable compatibility recipes

Store verified procedures locally and suggest them only when mod versions, hashes, configuration, and game version match. Revalidate recipes after updates.

## Principles for every milestone

1. Evidence over guesses; unknown is a valid result.
2. Overlap is not automatically a conflict.
3. Quality and correctness take priority over speed and feature count.
4. Performance work requires a measured bottleneck and regression protection.
5. ML follows real data quality and independent validation.
6. Local-first by default; no mandatory telemetry or cloud dependency.
7. Future repair tools preserve original mods and respect licenses.
8. No promise of crash-free gameplay.

---

*Milestones are considered complete only when their validation criteria have been met.*
