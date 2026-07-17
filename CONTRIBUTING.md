# Contributing to Kraken

Thanks for wanting to help. Kraken is a small, protocol-first tool: three
Claude Code skills, a handful of shell scripts, and a normative spec. That
shape decides how contributions work, so this page is short on purpose.

## What Kraken is (and how the repo is shaped)

Kraken ships **nothing you operate** — it's the protocol between a GitHub-Issues
task queue and the Claude Code workers that drain it. Three layers, and they
have a strict hierarchy when they disagree:

| Layer | Lives in | What it is |
| --- | --- | --- |
| **The spec** | [`PROTOCOL.md`](PROTOCOL.md) | The normative, agent-agnostic contract (`kraken-protocol/3`): task shape, the label state machine, the machine marker, the claim algorithm. **It wins on any disagreement.** |
| **The skills** | `skills/*/SKILL.md` | Prompts — markdown interpreted at runtime by an LLM. Prose here is *executable*: a subtle wording change can silently change an agent's behavior. |
| **The mechanics** | `skills/unleash/kraken.py`, `scripts/`, `tests/` | The deterministic parts — the bundled transition program (the reference implementation of the worker side, one stdlib-only module with a subcommand per transition), the linter, and the conformance suite. |

If a change to a skill and the spec ever conflict, the spec is the source of
truth; fix the skill (or amend the spec by PR — see below), never leave them
out of step. The linter enforces a lot of this mechanically.

## Dev setup

No build, no package manager — just `bash` and `jq`. A `Makefile` fronts the
checks. These two are token-free (no model calls, no network) and run in CI on
every PR:

```bash
make test    # conformance suite (bash tests/run-tests.sh) — needs jq
make lint    # deterministic skill lint (bash scripts/lint-skills.sh)
make check   # both of the above
```

- **`make test`** runs the conformance suite: each case drives the bundled
  transition program against a stateful `gh` stub, proving the queue protocol
  mechanically (the claim race, claim-window arbitration, honest release, …). It
  **requires `jq`**; without it the suite skips cleanly (exit 0), so it's safe on
  a minimal machine, but install `jq` to actually exercise it.
- **`make lint`** is the deterministic guard against the silent breakage a prose
  skill is exposed to: label drift across files, orphan "step N" references,
  task-template field drift, broken relative links/images, and unparseable
  shell/YAML/JSON snippets.

### The agent-behavior harness (run by hand)

`make test-agent` drives **real** `/kraken:unleash --once` runs (headless
`claude -p`) against the `gh` stub and asserts on artifacts — the skill's
*judgment*, not just the scripts. It is slow (several model runs) and **spends
tokens**, so it is deliberately **not** wired into any hook or CI. Run it by
hand when you change `skills/**` or `tests/agent/**`:

```bash
make test-agent   # KRAKEN_AGENT_ASSUME_AUTH=1 bash tests/agent/run-agent-tests.sh
```

It uses your logged-in Claude Code subscription (no paid API key) and self-skips
cleanly when it can't run for real (no `claude` on PATH, a spend/rate limit, or
the stub can't be reached).

A lighter **semantic** review still runs locally via the pre-push hook
(`.githooks/pre-push`): when a push touches `skills/**` or `README.md`, it asks
your logged-in `claude` CLI to review the diff against the skill invariants. It
costs no API bill. Enable it once per clone with:

```bash
git config core.hooksPath .githooks
```

## Pull request conventions

These are the conventions the history already follows — match them:

- **Conventional-commit subjects.** `feat(unleash): …`, `fix(unleash): …`,
  `docs(readme): …`, `ci: …`, `chore(release): …`. Imperative mood, short first
  line, body explaining the *why*.
- **One topic per PR.** Each PR does one thing and its title says what. Small,
  reviewable, single-purpose.
- **Branch names** follow a `type/NN-slug` shape keyed to a task or issue —
  e.g. `feat/7-mechanize-blocked-by`, `docs/6-why-not-x`,
  `fix/9-list-startable-pagination`, `ci/…`. CI pipelines key on these prefixes.
- **Everything in the repo is English** — files, comments, commit messages,
  branch names, PR titles and bodies. No exceptions.
- **Green checks.** The deterministic lint and conformance suite must pass; run
  them locally before you push.
- **Touching the protocol? Spec-first is a process rule, not a preference.** A
  behavior change to the coordination contract lands as a `PROTOCOL.md`
  amendment **plus a conformance test** (`tests/t/**` or `tests/unit/**`) in the
  same PR as — or before — the implementation. The spec is the source of truth:
  on any disagreement between spec, skills, scripts, and tests, **the spec
  wins**, and the fix brings the others back into line (never the reverse). A
  backward-incompatible change bumps the integer (`kraken-protocol/3` and
  onward); clarifications and strictly additive rules amend `PROTOCOL.md` in
  place by PR. Keep every skill, script, and the README consistent with the
  spec in the same change — the linter cross-checks labels, machine markers,
  and the attribution disclaimer across all of them.
- **Every normative clause is backed by a test.** `tests/COVERAGE.md` is the
  clause-by-clause audit mapping each `PROTOCOL.md` **MUST**/**SHOULD** to the
  test that pins it. When you amend the spec, add or update the pinning test and
  its `tests/COVERAGE.md` row in the same PR; a new normative clause with no
  test (or marked a gap without a follow-up issue) is not done. "The spec says
  it but no test pins it" is a defect: the reference implementation passing is
  not evidence a third-party implementation would.

## Where design discussion happens

Design lives in **GitHub Issues**. Open one before a large or ambiguous change
so the direction gets settled before code is written — an issue is cheaper to
redirect than a PR. Small, obvious fixes can go straight to a PR.

Kraken is also self-hosting: its own backlog is run as a Kraken queue, so a
well-shaped task issue (Goal / Acceptance / Notes) is itself a welcome
contribution — the queue is the front door, and a named worker may pick it up.

## Releasing (maintainers)

Releasing is a PR-gated flow — `main` is protected, so nothing publishes
without a merge:

1. **Cut the release.** Run **Actions → Release → Run workflow** and pick the
   bump type (patch / minor / major). That opens a `release/vX.Y.Z` PR bumping
   `.claude-plugin/plugin.json`. Merging it (the human approval) triggers
   `tag-release.yml`, which tags and publishes the [GitHub
   Release](https://github.com/rafael-adcp/kraken/releases) with notes
   auto-generated from the PRs merged since the last tag.

What changed between versions lives entirely in the GitHub Releases — there is
no hand-maintained changelog file. Because release notes come from PR titles,
write clear, descriptive PR titles, and call out protocol-affecting changes
explicitly (e.g. "implements kraken-protocol/3") so the `kraken@<version>`
commit trailer stays traceable to a protocol revision.
