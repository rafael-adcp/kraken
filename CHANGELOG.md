# Changelog

All notable changes to the Kraken plugin are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project versions the plugin (`.claude-plugin/plugin.json`) per
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Kraken is two things at once: a Claude Code **plugin** (versioned here) and an
agent-agnostic **coordination protocol** ([`PROTOCOL.md`](PROTOCOL.md),
currently `kraken-protocol/1`). The `Kraken-Task:` commit trailer carries
`kraken@<plugin-version>`; this changelog is the durable map from that version
back to the protocol revision it targets, so entries that change protocol
behavior say so explicitly ("implements kraken-protocol/1").

## [Unreleased]

<!-- Add entries here as they merge; the release flow promotes this section to a
     versioned heading. See CONTRIBUTING.md → "Releasing". -->

## [0.2.16] - 2026-07-15

### Added

- Marketplace discovery metadata (description, keywords, author) in the plugin
  manifest.
- Agent-behavior harness (`tests/agent/`) that drives real `/kraken:unleash`
  runs against the gh-stub to test the skill's judgment, not just the scripts.
- README "Why not just use X?" section positioning Kraken against Copilot's
  coding agent, Claude cloud/scheduled agents, and `claude-code-action` in CI.
- FAQ entry for hitting the Claude usage limit mid-task.

### Changed

- `list-startable.sh` is now the sole owner of the "startable" definition,
  so every consumer agrees on the same filter.
- The agent-behavior harness moved from scheduled CI (which burned a paid API
  key nightly) to a local pre-push hook driven by the logged-in `claude` CLI.

### Fixed

- The startable queue is now paginated, so more than 100 open tasks never
  truncate the candidate list.

## [0.2.15] - 2026-07-12

Targets `kraken-protocol/1` — **this release publishes and implements
kraken-protocol/1**, the versioned coordination spec. Every `kraken@0.2.15`
and later commit trailer maps back to this protocol revision.

### Added

- [`PROTOCOL.md`](PROTOCOL.md): `kraken-protocol/1`, the normative,
  agent-agnostic specification of the coordination contract (task shape, label
  state machine, machine lines, the claim algorithm, delivery, escalation,
  release).
- Bundled transition scripts under `skills/unleash/` (`claim.sh`,
  `heartbeat.sh`, `escalate.sh`, `deliver.sh`, `release.sh`,
  `list-startable.sh`) — the reference implementation of the worker side.
- Conformance suite (`tests/`) exercising the protocol's invariants against a
  stateful gh-stub: the claim guard, the claim race, claim-window arbitration,
  honest release, and failure staging.
- Canonical label colors and [`SECURITY.md`](SECURITY.md) (threat model).

### Changed

- The `watch` driver is bundled into `unleash`; the standalone `watch` skill is
  retired.
- `status` absorbs the launch-recon that `identify` used to do; the `identify`
  skill is retired.
- `SKILL.md` slimmed against the new spec — contract exposition moved into
  `PROTOCOL.md`, execution detail kept in the skill.

## [0.2.14] - 2026-07-11

### Changed

- README restructured for scanability and faster onboarding.

## [0.2.13] - 2026-07-11

### Added

- Non-canonical labels are stripped when a task closes (via the cleanup
  workflow), so closed issues read clean and label filters never match dead
  state.

### Changed

- Task template clarifies Goal vs Acceptance.

## [0.2.12] - 2026-07-09

### Added

- `init` and `status` skills, a placeholder guard, and a Concepts table in the
  README.

## [0.2.11] - 2026-07-09

### Changed

- README documentation for worker environments and the acceptance/QA step.

## [0.2.10] - 2026-07-09

### Added

- Event-driven queue watcher (`/kraken:watch`): a background poll that wakes the
  worker only when a startable task appears, replacing token-costly `/loop`
  polling.

## [0.2.9] - 2026-07-08

### Changed

- README pitch refreshed; worker-protocol duplication collapsed to a single
  source.

## [0.2.8] - 2026-07-08

### Added

- Skill lint (`scripts/lint-skills.sh`) — deterministic, token-free checks on
  the skill sources — and worker self-identification.

## [0.2.7] - 2026-07-07

### Changed

- Unleash worker protocol refined: per-task context passed to subagents, a
  safer claim, and a clearer commit-trailer slug.

## [0.2.6] - 2026-07-06

### Added

- Automation for the kraken cycle: an issue feeder, `/loop` documentation, and
  a trimmed task template.

## [0.2.5] - 2026-07-06

### Changed

- README polish: proof, badges, an updating section, and paste-and-run setup.

## [0.2.4] - 2026-07-06

### Added

- The `awaiting-merge` lifecycle: a task moves to `awaiting-merge` on delivery
  and closes when the work truly lands (the PR's `Closes` reference).

## [0.2.3] - 2026-07-05

### Fixed

- Deliveries respect the work repo's own branch-naming convention; traceability
  comes from commit trailers rather than a forced branch name.

## [0.2.2] - 2026-07-05

### Fixed

- Work delivery defined explicitly, and the queue listing hardened.

## [0.2.1] - 2026-07-05

### Fixed

- Two claim-protocol gaps closed and the `project:<name>` label documented.

### Changed

- Single source of truth: `WORKFLOW.md` folded into the README.

## [0.2.0] - 2026-07-05

### Changed

- `--worker-name` and `--project` are now mandatory when unleashing a worker.

[Unreleased]: https://github.com/rafael-adcp/kraken/compare/v0.2.16...HEAD
[0.2.16]: https://github.com/rafael-adcp/kraken/compare/v0.2.15...v0.2.16
[0.2.15]: https://github.com/rafael-adcp/kraken/compare/v0.2.14...v0.2.15
[0.2.14]: https://github.com/rafael-adcp/kraken/compare/v0.2.13...v0.2.14
[0.2.13]: https://github.com/rafael-adcp/kraken/compare/v0.2.12...v0.2.13
[0.2.12]: https://github.com/rafael-adcp/kraken/compare/v0.2.11...v0.2.12
[0.2.11]: https://github.com/rafael-adcp/kraken/compare/v0.2.10...v0.2.11
[0.2.10]: https://github.com/rafael-adcp/kraken/compare/v0.2.9...v0.2.10
[0.2.9]: https://github.com/rafael-adcp/kraken/compare/v0.2.8...v0.2.9
[0.2.8]: https://github.com/rafael-adcp/kraken/compare/v0.2.7...v0.2.8
[0.2.7]: https://github.com/rafael-adcp/kraken/compare/v0.2.6...v0.2.7
[0.2.6]: https://github.com/rafael-adcp/kraken/compare/v0.2.5...v0.2.6
[0.2.5]: https://github.com/rafael-adcp/kraken/compare/v0.2.4...v0.2.5
[0.2.4]: https://github.com/rafael-adcp/kraken/compare/v0.2.3...v0.2.4
[0.2.3]: https://github.com/rafael-adcp/kraken/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/rafael-adcp/kraken/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/rafael-adcp/kraken/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/rafael-adcp/kraken/releases/tag/v0.2.0
