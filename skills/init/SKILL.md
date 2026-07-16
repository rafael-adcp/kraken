---
name: init
description: Stand up a kraken coordination repo end to end — verify or create the private repo, install the bundled task template and coordination workflows (reaper, closed-issue cleanup, requeue-on-reply), and create the canonical labels. Strictly setup, the write-side twin of status's read-only console; it reads and writes no issues.
---

# Kraken — raise the head

You are the setup step for kraken: given a coordination-repo slug, you make that repo
ready to receive tasks — the private repo exists, the task template and coordination
workflows (reaper, closed-issue cleanup, requeue-on-reply) are committed, and the
state-machine labels are created. This is the symmetric partner
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

## Design decisions

- **Assets are copied from this skill's bundled folder, never fetched from the network.**
  `task-template.yml`, `reclaim-stale.yml`, `cleanup-closed.yml`, and
  `requeue-on-reply.yml` ship in this plugin's `skills/unleash/` folder — the same
  install as this skill (`unleash` resolves its bundled `watch-queue.sh` the same
  way). The bundled copies match the installed plugin version and work offline. Do
  not `curl` from `raw.githubusercontent.com`.
- **Files land via the GitHub contents API** (`gh api /repos/OWNER/tasks/contents/...`),
  not a clone — no temp dir, idempotent by content. For each asset: GET the path; on
  404, PUT it (create); if it exists and its content matches the bundled file, skip it;
  if it exists and differs, do NOT overwrite — flag it as customized and move on.
- **Idempotent throughout.** Re-running is safe: an existing repo and an already-installed
  asset are no-ops; an already-created label is **re-canonicalized in place** — its color
  and description reset to the bundled values via `--force` — never an error.
- **Labels carry a canonical color and description.** The GitHub labels UI *is* kraken's
  dashboard, so labels are not throwaway strings: a bare `gh label create` picks a
  **random** color, leaving every coordination repo with a differently-colored,
  undocumented state machine. `init` fixes both, so every kraken queue reads the same.

## Protocol

1. **Verify or create the repo.** Check it exists:

   ```
   gh repo view OWNER/tasks --json nameWithOwner --jq .nameWithOwner
   ```

   If that fails (repo absent), create it **private**:

   ```
   gh repo create OWNER/tasks --private
   ```

   Never create it public — the queue is instructions that run in your environment with
   your credentials (see the README's FAQ on who can command your workers).

2. **Install the four assets** into the coordination repo, resolving each from this
   skill's bundled `skills/unleash/` folder:

   - `task-template.yml` → `.github/ISSUE_TEMPLATE/task.yml`
   - `reclaim-stale.yml` → `.github/workflows/reclaim-stale.yml` (the reaper that
     reclaims dead workers' `in-progress` claims)
   - `cleanup-closed.yml` → `.github/workflows/cleanup-closed.yml` (strips a closed
     issue back to just `kraken-task` + `project:<name>`)
   - `requeue-on-reply.yml` → `.github/workflows/requeue-on-reply.yml` (requeues a
     held task when a genuine operator comment arrives — no 🐙 attribution
     disclaimer — so answering is "just reply")

   For each, check the destination via the contents API:

   ```
   gh api "/repos/OWNER/tasks/contents/.github/ISSUE_TEMPLATE/task.yml" \
     --jq .content 2>/dev/null | base64 -d
   ```

   - **404 (absent)** → create it. Base64-encode the bundled file and PUT it:

     ```
     gh api -X PUT "/repos/OWNER/tasks/contents/.github/ISSUE_TEMPLATE/task.yml" \
       -f message="chore: add kraken task template" \
       -f content="$(base64 < <bundled>/task-template.yml | tr -d '\n')"
     ```
   - **Present and byte-identical** to the bundled file → skip (no-op).
   - **Present but different** → do NOT overwrite. Report it as a customized file the
     operator kept on purpose, and continue.

3. **Create the canonical labels — each with its canonical color and description.** Use
   `--force` so the create **upserts**: it is idempotent on a fresh repo and it
   re-canonicalizes color/description drift on a re-run (an existing label is updated in
   place, never an error). Create all four:

   ```
   gh -R OWNER/tasks label create kraken-task    --force --color 1D76DB \
     --description "A unit of work for a kraken worker — the queue"
   gh -R OWNER/tasks label create in-progress    --force --color FBCA04 \
     --description "Claimed by a worker and being executed"
   gh -R OWNER/tasks label create needs-decision --force --color D93F0B \
     --description "Blocked on your decision — answer, then remove the label to requeue"
   gh -R OWNER/tasks label create awaiting-merge --force --color 0E8A16 \
     --description "Delivered as a draft PR — waiting for your review and merge"
   ```

   The colors trace the state machine left to right — blue *queued* → yellow *working* →
   red *needs you* / green *ready to land* — so the issues list reads as the flow in the
   README's diagram.

   When invoked with `--project <name>`, also create `project:<name>` the same way — the
   label a worker's `--project` filters on — in a distinct purple, so routing identity
   reads apart from state:

   ```
   gh -R OWNER/tasks label create "project:<name>" --force --color 5319E7 \
     --description "Canonical project identity — a worker's --project filters on this"
   ```

   Without `--project`, the four canonical labels are enough to stand the repo up; create
   the project label later (or let a re-run with `--project` do it).

4. **Print the settings reminder.** Do NOT write, create, or touch any
   `settings.json` — clobbering an existing one is a real footgun, and the permissions
   belong to each worker's prepared environment, not the coordination repo. Instead
   print a one-line reminder: before launching a worker, pre-allow the delivery commands
   in that environment's `.claude/settings.json` — see the worker-environment
   permissions example in `README.md`, which stays the source of truth.

5. **Nothing else.** No issues are read or written; no worker is launched. Point the
   operator at `status` (the queue's console, with ready-to-paste launch lines) or
   `unleash` (to start draining) as the next step.

## Authorization boundaries

- Invoking this skill is my authorization to, on the coordination repo only:
  (a) **create the repo private** if it does not exist;
  (b) **commit the four bundled template files** (`task.yml`, `reclaim-stale.yml`,
  `cleanup-closed.yml`, `requeue-on-reply.yml`) via the contents API, creating them
  only — never overwriting a file that already differs;
  (c) **create the canonical labels** (and the `project:<name>` label when `--project`
  is passed).
- It is NOT authorization to read or write issues, modify `settings.json`, delete
  anything, change repo visibility on an existing repo, or launch a worker.
- An existing file that differs from the bundled asset is flagged for me, never
  clobbered.

Coordination repo / flags / extra context: $ARGUMENTS
