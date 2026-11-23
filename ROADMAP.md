# BCI Development Roadmap

[English](ROADMAP.md) · [Русская версия](ROADMAP_RU.md) · [Project overview](README.md)

This roadmap describes the intended evolution of Bannerlord Compatibility Intelligence (BCI), from validating the current analyzer to evidence-based diagnostics and, eventually, assisted compatibility fixes.

**The roadmap is directional.** Version numbers are target milestones, not delivery-date promises. Scope and sequencing may change based on test results, technical constraints, and user needs.

## Current stage — 0.3.0-alpha: Analyzer QA

**Status: in progress**

The first priority is to establish what the current prototype does correctly on real Bannerlord installations.

Validation checklist:

- verify game-folder selection and module discovery;
- verify module metadata and load-order checks;
- inspect XML scan coverage and the correctness of reported findings;
- review warnings and distinguish real issues from harmless overlaps;
- verify HTML and JSON report generation;
- verify that reports are stored and can be reopened from the local SQLite database;
- test manual outcome labeling and the local training-data workflow;
- repeat scans and check for crashes, data loss, or inconsistent results.

**Exit criteria:** a documented QA report, a reproducible defect list, and a clear separation between verified behavior, known limitations, and unverified assumptions.

## 0.3.1-alpha — QA fixes

**Status: planned**

- fix defects found during 0.3.0-alpha testing;
- improve error handling and user-facing diagnostics;
- correct inaccurate or duplicated findings;
- add regression tests for fixed cases;
- rerun the same real-world test scenarios.

**Exit criteria:** previously discovered defects are either fixed and regression-tested or explicitly documented with a reason for deferral.

## 0.4.0-alpha — Core stabilization

**Status: planned**

- make scans repeatable and results consistent;
- attach findings to concrete files, XML locations, checks, and evidence;
- separate confirmed errors, potential risks, benign overrides, and unknown cases;
- deduplicate multiple warnings that originate from the same underlying issue;
- improve behavior on large mod collections and malformed or unusual files;
- establish a stable internal format for scan results and diagnostic evidence.

**Exit criteria:** the core analyzer produces traceable, understandable results and has regression coverage for the supported checks.

## 0.5.0-alpha — Compatibility evidence and test data

**Status: planned**

Build a reliable foundation for learning from real compatibility tests.

Each test record should capture, where available:

- Bannerlord and War Sails versions;
- mod identifiers, versions, hashes, and load order;
- relevant configuration and test scenario;
- analyzer findings;
- in-game outcome and reproduction steps;
- evidence status: confirmed, not reproduced in the tested scenario, potential, or unknown.

Introduce controlled A/B/A testing: record a baseline, change a specific mod or combination, test the result, revert the change, and repeat the scenario. A/B/A results are evidence, not automatic proof of causality; uncontrolled changes must be documented.

**Exit criteria:** test cases are reproducible, deduplicated, versioned, and clearly separated from synthetic parser fixtures.

## 0.6.0-alpha — Deeper static analysis

**Status: planned**

Expand analysis beyond basic file and XML checks, in incremental and testable steps:

- semantic XML analysis, including values, references, and data relationships;
- XSLT transformations;
- missing or conflicting resources and assets;
- DLL inspection: types, methods, and game API dependencies;
- Harmony patch targets, patch types, and ordering where it can be established;
- evidence linking each finding to the relevant source location.

An overlap, shared method, or patched target must not be labeled a conflict without evidence of an incompatible effect.

**Exit criteria:** each new analyzer family has fixtures, documented limits, and tests against real mod examples before being presented as a reliable diagnostic.

## 0.7.0-alpha — Validated machine learning

**Status: planned**

Introduce ML only after the project has enough trustworthy outcome data.

- train a first LightGBM model on locally collected and reviewed examples;
- keep synthetic fixtures separate from real gameplay outcomes;
- evaluate on unseen mods, pairs, builds, or versions using group-based splits;
- measure precision, recall, false positives, false negatives, and calibration;
- compare predictions with deterministic analyzer findings;
- begin in **shadow mode**, without changing the deterministic Risk Index or assigning blame to individual mods.

No model score should be presented as a calibrated probability until calibration has been measured and demonstrated.

**Exit criteria:** documented validation results, known limitations, and a clear decision about whether the model adds value beyond rule-based analysis.

## 0.8.0-beta — Interaction and runtime diagnostics

**Status: planned**

- analyze interactions between pairs and groups of mods;
- collect and interpret relevant game logs, exceptions, and crash information where available;
- build an evidence graph connecting mods, files, patches, runtime events, and test outcomes;
- rank investigation leads using evidence and controlled interventions;
- explain why a particular combination is suspicious and what evidence supports that conclusion.

The system must distinguish correlation from demonstrated cause. A model ranking alone is not proof that a mod is responsible.

**Exit criteria:** interaction findings can be traced to evidence and reproduced test scenarios, with uncertainty clearly communicated.

## 0.9.0 — Release candidate and distribution

**Status: planned**

- produce and verify a Windows x64 installer on a clean system;
- test installation, launch, upgrade, and uninstall paths;
- make local data storage, backup, and recovery dependable;
- separate application, rules, and model updates;
- support rollback of unsuccessful updates;
- complete user documentation and known-limitations notes;
- run regression tests on supported Bannerlord configurations.

**Exit criteria:** repeatable installer builds, successful clean-machine tests, no unresolved release-blocking defects, and complete release notes.

## 1.0.0 — First stable release

**Status: long-term target**

BCI 1.0 should mean that the documented core functionality is reliable and its limitations are understood. It does **not** mean that every mod combination is compatible or that crashes are impossible.

A stable release requires:

- repeatable scans and dependable local report storage;
- evidence-backed findings with clear confidence and uncertainty;
- regression-tested supported checks;
- verified installation and update procedures;
- documentation of supported environments and known limitations;
- quality claims supported by test results.

## Beyond 1.0 — Long-term directions

These ideas are intentionally outside the current alpha scope.

### BCI Compatibility Forge

Create a separate generated compatibility module (for example, \`BCI_Compatibility\`) containing carefully selected patches, adapters, configuration, and a manifest. Original mods should remain untouched. Users should be able to review trade-offs, choose the intended behavior, test the result, and roll it back.

Start with safe, independent data changes. Complex DLL, Harmony, and runtime interactions may require custom adapters or author involvement and cannot always be resolved by choosing one side of a conflict. Respect mod licenses and redistribution restrictions.

### Optional local AI assistant

An optional local language model may help explain complex findings, summarize logs, or draft candidate fixes. It must remain optional and must not replace deterministic checks, test evidence, or user approval.

### Reusable compatibility recipes

Store verified resolution procedures locally and suggest them again only when the relevant mod versions, hashes, configuration, and game version match. Revalidate recipes after updates.

## Principles that apply to every milestone

1. **Evidence first:** every important finding should be traceable to evidence.
2. **No overlap-equals-conflict shortcuts:** shared files or methods are not automatically defects.
3. **Unknown is a valid result:** do not invent certainty where tests are missing.
4. **ML follows data quality:** predictions remain experimental until independently validated.
5. **Local-first by default:** no mandatory cloud dependency or telemetry by default.
6. **Protect original mods:** future repair features should use separate compatibility modules and respect licenses.
7. **No crash-free guarantees:** the goal is to reduce risk and improve diagnosis, not promise perfection.

---

*This roadmap is maintained as the project evolves. Completed milestones will be updated only after their validation criteria have been met.*