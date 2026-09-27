# Contributing to Kraken

Thanks for wanting to help. Kraken is a small, protocol-first tool: three
agent skills (Claude Code plugin format; a GitHub Copilot CLI worker follows the
same `SKILL.md` through [`AGENTS.md`](AGENTS.md)), a handful of shell scripts,
and a normative spec. That
shape decides how contributions work, so this page is short on purpose.

## What Kraken is (and how the repo is shaped)

Kraken ships **nothing you operate** — it's the protocol between a GitHub-Issues
task queue and the agent workers (Claude Code, GitHub Copilot CLI) that drain it. Three layers, and they
have a strict hierarchy when they disagree:

| Layer | Lives in | What it is |
| --- | --- | --- |
| **The spec** | [`PROTOCOL.md`](PROTOCOL.md) | The normative, agent-agnostic contract (`kraken-protocol/9`): task shape, the label state machine, the machine marker, the claim algorithm. **It wins on any disagreement.** |
| **The skills** | `skills/*/SKILL.md` | Prompts — markdown interpreted at runtime by an LLM. Prose here is *executable*: a subtle wording change can silently change an agent's behavior. |
| **The mechanics** | `skills/unleash/kraken.py`, `scripts/`, `tests/` | The deterministic parts — the bundled transition program (the reference implementation of the worker side — `kraken.py` is the entry point, the `kraken/` package beside it the stdlib-only implementation, one subcommand per transition), the linter, and the conformance suite. |

If a change to a skill and the spec ever conflict, the spec is the source of
truth; fix the skill (or amend the spec by PR — see below), never leave them
out of step. The linter enforces a lot of this mechanically.

## Dev setup

No build, no package manager — just `bash` and `jq`. A `Makefile` fronts the
checks. These are token-free (no model calls, no network) and run in CI on
every PR:

```bash
make test       # conformance + unit suites (native `python3 -m unittest`) — stdlib only
make test-e2e   # the real Copilot CLI against a scripted fake model (BYOK, offline)
make lint       # deterministic skill lint (bash scripts/lint-skills.sh)
make check      # all of the above
```

- **`make test`** runs the native stdlib runner — two `python3 -m unittest
  discover` passes over `tests/unit/` and `tests/conformance/`. The conformance
  suite drives the bundled transition program against an in-process HTTP stub of
  GitHub's REST + GraphQL API (`tests/gh-stub/server.py`), reached over
  `GITHUB_API_URL` — the same transport seam kraken.py uses in production —
  proving the queue protocol mechanically (the claim-ref CAS race, thread
  independence, the reconciler, honest release, …); the `tests/unit/` pass covers
  the `kraken.py` units. Both suites are **stdlib only** (no `jq`, no `gh`), so
  they run on a minimal machine.
- **`make lint`** is the deterministic guard against the silent breakage a prose
  skill is exposed to: label drift across files, orphan "step N" references,
  task-template field drift, broken relative links/images, and unparseable
  shell/YAML/JSON snippets.
- **`make test-e2e`** runs the **real** GitHub Copilot CLI in BYOK offline mode
  (`COPILOT_PROVIDER_BASE_URL` + `COPILOT_OFFLINE=true`) against a scripted,
  OpenAI-compatible fake model (`tests/fake-model/model_server.py`). The "model"
  plays a fixed list of tool calls, so the run is deterministic and needs no
  credentials. Everything around the model is real: `scripts/kraken-loop.sh`, the
  prompt and deny rules in `scripts/lib-copilot-drain.sh`, Copilot's shell tool
  and permission layer, the plugin's `hooks.json` (SessionEnd), `kraken.py`
  against the stub, and a git work repo with a bare remote. It proves the
  **wiring**, not the model's judgment: a whole drain to a draft PR, the deny
  rules blocking merge/delete/close, and SessionEnd releasing only its own
  session's claim. It needs `copilot` on PATH (`npm install -g @github/copilot`)
  and skips without it; CI pins the CLI version and sets `KRAKEN_E2E_REQUIRE=1`,
  so there a missing CLI fails instead of skipping.

### The agent-behavior harness (run by hand)

`make test-agent` drives **real** `/kraken:unleash --once` runs (headless
`claude -p`) against the `gh` stub and asserts on artifacts — the skill's
*judgment*, not just the scripts. It is slow (several model runs) and **spends
tokens**, so it is deliberately **not** wired into any hook or CI. Run it by
hand when you change `skills/**` or `tests/agent/**`:

```bash
make test-agent           # on Claude Code
make test-agent-copilot   # the same scenarios on GitHub Copilot CLI
```

It uses your logged-in Claude Code subscription (or `copilot login`; no paid API
key) and self-skips cleanly when it can't run for real (the CLI not on PATH, a
spend/rate limit, or the stub can't be reached). A change to `AGENTS.md`,
`scripts/kraken-loop.sh`, `scripts/kraken-copilot.sh` or
`scripts/lib-copilot-drain.sh` is a Copilot-side change: run
`make test-agent-copilot` for it.

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
  amendment **plus a conformance test** (`tests/conformance/**` or `tests/unit/**`) in the
  same PR as — or before — the implementation. The spec is the source of truth:
  on any disagreement between spec, skills, scripts, and tests, **the spec
  wins**, and the fix brings the others back into line (never the reverse). A
  backward-incompatible change bumps the integer (`kraken-protocol/9` and
  onward) **and adds an entry to [`HISTORY.md`](HISTORY.md)** — what changed,
  what the previous design forced, whether it breaks compatibility, and what it
  retired; clarifications and strictly additive rules amend `PROTOCOL.md` in
  place by PR and need no entry. Keep every skill, script, and the README
  consistent with the spec in the same change — the linter cross-checks labels,
  machine markers, and the attribution disclaimer across all of them.
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
explicitly (e.g. "implements kraken-protocol/9") so the `kraken@<version>`
commit trailer stays traceable to a protocol revision.
