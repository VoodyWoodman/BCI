# Bannerlord Compatibility Intelligence (BCI)

**A local-first project for analyzing mod compatibility in Mount & Blade II: Bannerlord.**

[English](README.md) · [Русская версия](README_RU.md)

[Development roadmap](ROADMAP.md) · [Дорожная карта](ROADMAP_RU.md)

BCI is being developed to help players understand how Bannerlord mods may interact, identify potential compatibility risks, and investigate issues using technical evidence and controlled in-game tests.

The long-term goal is to move beyond simple file-overlap warnings toward evidence-based diagnostics: explain what may be affected, show why a combination is suspicious, and help users evaluate possible ways to resolve problems.

## Project status

**Current stage: 0.3.0-alpha — analyzer QA and validation.**

The current Windows x64 prototype launches locally and is undergoing functional testing. Its existing scope includes early module and load-order checks, XML analysis, local HTML/JSON reports, a local SQLite report store, and components for recording test outcomes and experimenting with model training.

This is an early development build, not a stable release. The accuracy and coverage of the analyzer are still being evaluated against real Bannerlord mod setups. The Windows installer workflow has not yet produced a verified release package, and there are currently no stable downloads published on GitHub.

See the [0.3.0-alpha development branch](https://github.com/VoodyWoodman/BCI/tree/release/0.3.0-alpha) and [GitHub Actions build history](https://github.com/VoodyWoodman/BCI/actions).

## Product principles

- **Evidence over guesses.** Findings should point to concrete files, data, rules, logs, or reproducible tests.
- **Overlap is not automatically a conflict.** Two mods touching the same file, XML node, or method do not necessarily break each other.
- **Clear uncertainty.** Confirmed defects, potential risks, harmless overrides, and unknown cases must be distinguished.
- **Local-first.** The intended default is local analysis and local data storage, without mandatory cloud services.
- **Human decisions stay with the user.** Recommendations should explain trade-offs rather than silently changing a mod setup.
- **No impossible promises.** BCI aims to reduce compatibility problems; it cannot guarantee a crash-free game.

## Roadmap

The roadmap is published in both languages:

- [Development roadmap — English](ROADMAP.md)
- [Дорожная карта — русский](ROADMAP_RU.md)

Version numbers in the roadmap are planning milestones, not release-date commitments. Each stage depends on the results and quality gates of the previous one.

## Planned capabilities

As the analyzer matures, the project may expand into:

- deeper XML and data-reference analysis;
- XSLT, asset, DLL/API, and Harmony patch inspection;
- structured compatibility evidence and controlled A/B/A test records;
- ML-assisted risk assessment, introduced only after validation and initially used in shadow mode;
- analysis of interactions between pairs and groups of mods;
- clear Simple and Advanced presentation modes;
- a separate generated compatibility module through **BCI Compatibility Forge**, while keeping original mods untouched;
- optional local AI assistance for explaining complex findings.

These are development directions, not claims that the features are already implemented.

## Current limitations

- BCI is in alpha and its findings require verification.
- A warning is not proof that a mod combination is broken.
- There is no validated classifier trained on a sufficiently representative set of real Bannerlord gameplay outcomes yet.
- ML predictions must not be treated as confirmed causes.
- Compatibility Forge and the local language-model assistant are long-term ideas, not current alpha features.

## Development

The immediate priority is to validate the existing analyzer on real mod installations, document false positives and missed issues, and fix defects before expanding the analysis scope.

For the planned sequence of work and validation gates, see the [roadmap](ROADMAP.md).

---

*Bannerlord Compatibility Intelligence is an independent community project and is not affiliated with or endorsed by TaleWorlds Entertainment.*