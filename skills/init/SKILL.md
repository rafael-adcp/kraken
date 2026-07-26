---
name: init
description: Stand up a kraken coordination repo end to end — verify or create the private repo, install the bundled task template, remove everything an earlier release installed to be executed server-side, and create the canonical labels. Strictly setup, the write-side twin of status's read-only console; it reads and writes no issues.
---

# Kraken — raise the head

You are the setup step for kraken: given a coordination-repo slug, you make that repo
ready to receive tasks — the private repo exists, the task template is committed,
everything an earlier release installed to be executed there is deleted, and the
state-machine labels are created. Under `kraken-protocol/7` the coordination repo
runs nothing of its own, so it needs no GitHub Actions and carries no copy of the
transition program. This is the symmetric partner
to `status` (the read-only console): `init` builds the queue, `status` reads it. You
touch no issues — none read, none written.

## Invocation

```
/kraken:init OWNER/tasks [--project <name>]
```

The `OWNER/tasks` argument is REQUIRED — the coordination repo to stand up. Missing? Do
not guess. Ask for it and stop.

If the slug matches `^OWNER/` or contains `<`/`>`, refuse: it looks like the template
placeholder — substitute your real `owner/repo` and re-run.

`--project <name>` is optional. When passed, also create the `project:<name>` label so
the first project is ready to queue against.

There is no repair flag, and no version handshake between the plugin and the
coordination repo: under `kraken-protocol/7` the repo holds no copy of the transition
program, so there is no second copy to fall out of date.

## Design decisions

- **The mechanics live in `kraken.py init`, not in this prose.** Standing a repo
  up — verify-or-create it private, install the bundled assets, upsert the
  canonical labels — is deterministic, zero-judgment work, so it is executed by
  the bundled `kraken.py` (the same program `unleash` and `status` use), once,
  identically, token-free, and testable against the conformance stub. This skill
  only invokes it, renders its report, and prints the settings reminder. Same
  rule that produced `claim-next` and `status`.
- **Assets ship in the plugin, never fetched from the network.**
  `task-template.yml` lives in this plugin's `skills/unleash/` folder —
  `kraken.py init` reads it from there and commits it via the GitHub contents
  API. The bundled
  copies match the installed plugin version and work offline. Never `curl` from
  `raw.githubusercontent.com`.
- **Idempotent and non-destructive by construction.** `kraken.py init` creates
  the repo only if absent, creates each asset only if absent, and upserts the
  labels with their canonical color/description. An existing file is reported
  `present` and left exactly as it is. Re-running is safe.
- **Nothing installed is executed, so nothing has to stay in sync.** The one
  asset is an issue form — it shapes a task on the way in and runs no code.
  A template the operator tuned is their business, not drift to police, which is
  why there is no `--upgrade`, no byte comparison and no refusal path.
- **Re-running init is the migration.** A coordination repo stood up by an older
  release still carries the workflows and the vendored program that release
  installed; a cron left running against retired semantics keeps mutating labels
  behind every worker's back. So init DELETES them — gated on kraken's own file
  header, so something the operator wrote at one of those paths is never
  destroyed by a bootstrap command.

## Protocol

1. **Run the bootstrap.** `kraken.py init` does the whole deterministic gesture —
   verify-or-create the repo **private**, install the bundled `task.yml`
   issue-form template via the contents API, delete everything an earlier release
   installed to be executed server-side, and upsert the canonical state-machine
   labels (`kraken-task`, `in-progress`,
   `needs-decision`, `awaiting-merge`) plus the `priority:high` scheduling label
   with their canonical colors and descriptions:

   ```
   python3 "<this skill's folder>/../unleash/kraken.py" init OWNER/tasks [--project <name>]
   ```

   Pass `--project <name>` to also upsert the `project:<name>` routing label a
   worker's `--project` filters on. Branch on the exit
   code: `0` — bootstrapped (render its report); `20` — a gh/network failure,
   state may be partial, re-run after checking (init is idempotent, so a re-run
   resumes safely).

2. **Render the report.** `kraken.py init` prints one line per repo/asset/label
   decision (`created` / `present` / `removed` / `upserted`) and a summary line.
   Relay it. Call out anything reported **removed** — that is a workflow (or the
   vendored program) an earlier release installed and this protocol revision
   retired, deleted so it stops running against semantics the workers no longer
   implement.

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
  (b) **commit the bundled `task.yml` template** via the contents API, creating
  it only — never overwriting a file that already exists;
  (b2) **delete everything an earlier release installed to run server-side**
  (`reclaim-stale.yml`, `requeue-on-reply.yml`, `cleanup-closed.yml`,
  `validate-task.yml`, and the vendored `.github/kraken.py` they exec'd — their
  jobs are now derived by the reader on the queue read it already performs,
  PROTOCOL.md §2.1 and §6), and only when the file still carries kraken's own
  header, so something I wrote at that path is never touched;
  (c) **upsert the canonical labels** (and the `project:<name>` label when
  `--project` is passed).
- It is NOT authorization to read or write issues, modify `settings.json`, delete
  anything, change repo visibility on an existing repo, or launch a worker.
- init never overwrites an existing file, and never deletes one that does not
  carry kraken's own header — `kraken.py init` enforces both, it is not left to
  judgment.

Coordination repo / flags / extra context: $ARGUMENTS
