---
name: init
description: Stand up a kraken coordination repo end to end — verify or create the private repo, install the bundled task template and coordination workflows (reaper, closed-issue cleanup, requeue-on-reply, queue-entry validator), and create the canonical labels. Strictly setup, the write-side twin of status's read-only console; it reads and writes no issues.
---

# Kraken — raise the head

You are the setup step for kraken: given a coordination-repo slug, you make that repo
ready to receive tasks — the private repo exists, the task template and coordination
workflows (reaper, closed-issue cleanup, requeue-on-reply, queue-entry validator) are
committed, and the
state-machine labels are created. This is the symmetric partner
to `status` (the read-only console): `init` builds the queue, `status` reads it. You
touch no issues — none read, none written.

## Invocation

```
/kraken:init OWNER/tasks [--project <name>] [--upgrade]
```

The `OWNER/tasks` argument is REQUIRED — the coordination repo to stand up. Missing? Do
not guess. Ask for it and stop.

If the slug matches `^OWNER/` or contains `<`/`>`, refuse: it looks like the template
placeholder — substitute your real `owner/repo` and re-run.

`--project <name>` is optional. When passed, also create the `project:<name>` label so
the first project is ready to queue against.

`--upgrade` is optional. Plain `init` is create-only and never overwrites an installed
asset; `--upgrade` is the **repair path** — it replaces any vendored asset whose bytes
match a **known previous release** (so it was shipped by the plugin and never
hand-edited) with the current bundled copy. An asset matching **no** release is a
deliberate customization and is still left untouched. Use it after a plugin upgrade to
pull a coordination repo's vendored assets forward.

## Design decisions

- **The mechanics live in `kraken.py init`, not in this prose.** Standing a repo
  up — verify-or-create it private, install the bundled assets, upsert the
  canonical labels — is deterministic, zero-judgment work, so it is executed by
  the bundled `kraken.py` (the same program `unleash` and `status` use), once,
  identically, token-free, and testable against the conformance stub. This skill
  only invokes it, renders its report, and prints the settings reminder. Same
  rule that produced `claim-next` and `status`.
- **Assets ship in the plugin, never fetched from the network.**
  `task-template.yml`, `kraken.py`, `reclaim-stale.yml`, `cleanup-closed.yml`,
  `requeue-on-reply.yml`, and `validate-task.yml` live in this plugin's
  `skills/unleash/` folder — `kraken.py init` reads them from there and commits
  them via the GitHub contents API. `kraken.py` itself is vendored as a sixth
  asset (`.github/kraken.py`) so the coordination workflows exec one parser
  instead of re-implementing the protocol parse in jq/grep/awk. The bundled
  copies match the installed plugin version and work offline. Never `curl` from
  `raw.githubusercontent.com`.
- **Idempotent and non-destructive by construction.** `kraken.py init` creates
  the repo only if absent, creates each asset only if absent, and upserts the
  labels with their canonical color/description. A **differing** file is
  classified — `outdated` (its bytes match an older release, so it drifted behind
  a plugin upgrade) or `customized` (its bytes match no release, a deliberate
  edit) — and never overwritten by a plain run. Re-running is safe.
- **`--upgrade` repairs drift, never clobbers intent.** An asset manifest in
  `kraken.py` records the hash of every released version of each bundled asset.
  `--upgrade` replaces only the `outdated` assets (a known release, so safe to
  advance), leaving `customized` ones for the operator. This is the fix the drain
  points at when its protocol-version handshake finds a repo running stale
  vendored assets.

## Protocol

1. **Run the bootstrap.** `kraken.py init` does the whole deterministic gesture —
   verify-or-create the repo **private**, install the six bundled assets
   (`task.yml` template + the vendored `kraken.py` transition program + the
   `reclaim-stale`, `cleanup-closed`, `requeue-on-reply`, `validate-task`
   workflows) via the contents API, and
   upsert the canonical state-machine labels (`kraken-task`, `in-progress`,
   `needs-decision`, `awaiting-merge`) with their canonical colors and
   descriptions:

   ```
   python3 "<this skill's folder>/../unleash/kraken.py" init OWNER/tasks [--project <name>] [--upgrade]
   ```

   Pass `--project <name>` to also upsert the `project:<name>` routing label a
   worker's `--project` filters on. Pass `--upgrade` to additionally replace any
   `outdated` vendored asset (one whose bytes match a known older release) with
   the bundled copy — the repair path after a plugin upgrade. Branch on the exit
   code: `0` — bootstrapped (render its report); `20` — a gh/network failure,
   state may be partial, re-run after checking (init is idempotent, so a re-run
   resumes safely).

2. **Render the report.** `kraken.py init` prints one line per repo/asset/label
   decision (`created` / `unchanged` / `outdated` / `upgraded` / `customized` /
   `upserted`) and a summary line. Relay it, and call out any asset reported
   **customized** — a file the operator changed on purpose that init left
   untouched (installing the bundled version there is a manual choice, not init's
   to make) — and any **outdated** — a shipped asset that drifted behind a plugin
   upgrade, which a re-run with `--upgrade` will reinstall.

3. **Print the settings reminder.** Do NOT write, create, or touch any
   `settings.json` — clobbering an existing one is a real footgun, and the
   permissions belong to each worker's prepared environment, not the coordination
   repo. Instead print a one-line reminder: before launching a worker, pre-allow
   the delivery commands in that environment's `.claude/settings.json` — see the
   worker-environment permissions example in `README.md`, which stays the source
   of truth.

4. **Nothing else.** No issues are read or written; no worker is launched. Point
   the operator at `status` (the queue's console, with ready-to-paste launch
   lines) or `unleash` (to start draining) as the next step.

## Authorization boundaries

- Invoking this skill is my authorization to, on the coordination repo only:
  (a) **create the repo private** if it does not exist;
  (b) **commit the six bundled template files** (`task.yml`, `kraken.py`,
  `reclaim-stale.yml`, `cleanup-closed.yml`, `requeue-on-reply.yml`,
  `validate-task.yml`) via the contents API, creating them only — never
  overwriting a file that already differs — **except** that a `--upgrade` run may
  replace an asset whose bytes match a known older release (never a `customized`
  one);
  (c) **upsert the canonical labels** (and the `project:<name>` label when
  `--project` is passed).
- It is NOT authorization to read or write issues, modify `settings.json`, delete
  anything, change repo visibility on an existing repo, or launch a worker.
- An existing file that matches no known release (customized) is flagged for me,
  never clobbered — `kraken.py init` enforces this even under `--upgrade`, it is
  not left to judgment.

Coordination repo / flags / extra context: $ARGUMENTS
