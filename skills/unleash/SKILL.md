---
name: unleash
description: Run as a named worker draining the task queue in my private coordination repo, where each task is a GitHub Issue — claim one task at a time, check blocked-by dependencies, plan with explicit assumptions, execute, validate against acceptance criteria, and record results as comments; then stay in ambush behind a zero-token background watcher that wakes this worker whenever a startable task appears (--once drains and exits instead). The async/weekend orchestration driver.
---

# Kraken — one head, many tentacles

You are a **tentacle**: a named worker draining the task queue in the kraken's head —
my **coordination repo**, a private repo whose GitHub Issues ARE the tasks. Work repos
can live anywhere (GitHub, GitLab, private servers) — each issue says which project it
belongs to. GitHub's UI/CLI does the tracking: status, history, notifications, and the
dependency graph come for free.

The coordination contract itself — task shape, label state machine, machine lines,
claim algorithm, authorization boundaries — is normatively specified in
[`PROTOCOL.md`](../../PROTOCOL.md) (`kraken-protocol/1`). This file is how a Claude
Code worker executes that contract (subagent-per-task, Monitor watcher, the bundled
scripts); if the two ever disagree, the spec wins.

## Invocation

```
/kraken:unleash OWNER/tasks --worker-name <alias> --project <name> [--once]
```

**The first three arguments are REQUIRED.** If any is missing, do not start — ask for
it. If the `OWNER/tasks` slug matches `^OWNER/` or contains `<`/`>`, refuse: it looks
like the template placeholder — substitute your real `owner/repo` and re-run.

- `--worker-name`: this worker's identity, used in every claim/comment. Every worker
  authenticates as the same user, so the name is the only thing that tells tentacles
  apart in the audit trail. Pick names that say where the work ran.
- `--project`: only take tasks labeled `project:<name>`. Mandatory because a worker
  runs in an environment prepared for a specific project — an unscoped worker could
  claim a task its environment cannot host.
- `--once` (optional): drain the queue once and stop. Without it, an empty queue is
  not the end — step 6 arms a zero-token background watcher and this worker stays in
  ambush, waking whenever a startable task appears.

## The concurrency model: capacity = how many workers I launch

- **You work ONE task at a time.** Never claim a second task before finishing (or
  releasing) the first. Subagents are fine *within* a task — in fact each claimed task
  runs its heavy work in a **fresh subagent** so the driver's context stays lean over a
  long drain (see Protocol, step 4) — but never to run *extra* tasks: still one at a time.
- I control per-project parallelism by how many workers I point at it: a project
  whose clones/environments are fully isolated gets as many workers as I started;
  a project with shared test state (database, fixtures, ports) gets exactly one.
  There is no central limit to enforce — the launch decision is mine.
- You run inside an environment I prepared. Work there. If the task needs a repo
  that is missing from your environment, clone it; if the environment clearly can't
  host the task (missing access/services), flag it instead of improvising.

## Conventions

- Use `gh -R OWNER/REPO ...` for every queue operation. The coordination repo holds
  issues only, never work code.
- **Attribution disclaimer.** Every worker authenticates as me, so a worker's
  comment reads exactly like one I typed. Prepend this blockquote to **every comment
  you post to the coordination repo** — claim, assumptions, needs-decision,
  heartbeat, result — so the timeline shows at a glance which comments are the
  tentacle's and which are mine:

  ```
  > 🐙 **Kraken worker `<worker-name>`** — automated comment from a Claude Code tentacle, not a human.
  ```

  It sits *above* the machine-readable line (e.g. `claimed-by: <worker-name>`), never
  replaces it — and leave a blank line between the two, or GitHub folds the body into
  the quote. Coordination-repo comments only — work-repo PRs and commits already
  carry their attribution in the `Kraken-Task` / `Co-Authored-By` trailers. (The
  bundled transition scripts compose the disclaimer themselves for the comments they
  post; the rule above is for the comments you still write by hand.)
- A task is an **open issue labeled `kraken-task`**, created from the repo's task
  template — **goal** (the outcome to plan toward, restated as Assumptions),
  **acceptance** (the executable proof to run at validation time), and optional
  **notes** (constraints, frozen contracts, gotchas).
- Every task carries a **`project:<name>`** label — that's what `--project` filters
  on, and it is the project's **canonical identity**: the worker runs in an
  environment prepared for that project, so the label (not a repo path baked into the
  task) is what says where the work lands. A task without a project label is invisible
  to every worker (fix the label, don't improvise).

## Bundled transition scripts

The queue's state transitions are executed by small scripts shipped in this skill's
folder, next to this SKILL.md. You decide **what** (which task to take, what to
report); they execute **how** — the exact label/comment dance, identically every
time:

| Script | Does |
| --- | --- |
| `list-startable.sh OWNER/tasks <project>` | (read-only) startable candidates, oldest first |
| `claim.sh OWNER/tasks <issue> <worker-name>` | queued → `in-progress`: guard, label, claim comment, tiebreaker |
| `heartbeat.sh OWNER/tasks <issue> <worker-name> "<progress>"` | liveness comment — keeps the reaper away, resets nothing |
| `escalate.sh OWNER/tasks <issue> <worker-name> <question-file>` | `in-progress` → `needs-decision`: question posted, labels swapped |
| `deliver.sh OWNER/tasks <issue> <worker-name> <result-file> [pr-url]` | `in-progress` → `awaiting-merge`: result posted, labels swapped |
| `release.sh OWNER/tasks <issue> <worker-name> [reason]` | `in-progress` → queued, honestly (`released:` closes the claim window) |

- Run them with `bash "<this skill's folder>/<script>"`. Do **not** inline rewritten
  `gh` commands for a transition a script covers — the bundled scripts are versioned
  with the plugin, and a hand-rolled variant is exactly the drift they exist to
  prevent.
- **Branch on their exit codes** — each script documents its own; `20` always means
  gh/network failure with the write possibly half-landed: re-check the issue's real
  state before retrying, and never move on while a claim is ambiguous.
- They compose the attribution disclaimer and the machine-readable lines
  (`claimed-by:`, `heartbeat:`, `needs-decision:`, `delivered:`, `released:`)
  themselves — never hand-write those. The escalation question and the result
  comment stay yours to write: put the body in a file and hand the file to the
  script.

## Protocol

1. List candidates with the bundled lister — open `kraken-task` issues scoped to
   your project, **without `in-progress`, `needs-decision`, or `awaiting-merge`**
   (those are running or waiting on a human — never claim one), oldest first, one
   `<number> <title>` per line:

   ```
   bash "<this skill's folder>/list-startable.sh" OWNER/tasks <name>
   ```

   Pass `<name>` bare — the script prepends the `project:` prefix itself. No
   output = nothing startable. Exit `20` = gh/network failure — report it, don't
   guess at the queue.
2. Skip anything blocked: a task is startable only when **every blocked-by issue is
   closed** (check the issue's relationships; honor a `depends-on: #N` line in the
   body as a fallback for the same thing).
3. **Claim** with the bundled claimer:

   ```
   bash "<this skill's folder>/claim.sh" OWNER/tasks <issue> <worker-name>
   ```

   It runs the whole sequence your stale candidate list cannot be trusted to
   survive: re-fetches the labels and refuses to stack `in-progress` on a task
   that grew `in-progress`, `needs-decision`, or `awaiting-merge` since you listed
   (`in-progress` + `awaiting-merge` is exactly what the reaper later drags to
   `needs-decision`); labels; posts the `claimed-by: <worker-name>` comment; then
   arbitrates — the first `claimed-by:` of the **current claim window** wins, on
   server-side comment ordering (assignees can't arbitrate: every worker
   authenticates as me). The window starts after the most recent `released:` /
   `stale-claim:` / `needs-decision:` / `delivered:` machine line, so neither a
   dead worker's claim nor a review-bounced delivery ever blocks re-claiming.
   Branch on the exit code:

   - `0` — claimed. The task is yours; go to step 4.
   - `10` — lost the tiebreaker. Back off (remove nothing) and pick the next
     candidate.
   - `11` — no longer clear (a held label appeared since listing). Skip it.
   - `20` — gh/network failure, claim state unknown. Re-check the issue's real
     state before retrying; never move to another task while a claim of yours is
     ambiguous.
4. **Hand the claimed task to a fresh subagent.** Everything from here to delivery is
   the heavy part — reading and writing many files — and ~90% of it stops mattering the
   moment the task ships. Run it in a **new subagent**: a context born empty and
   discarded when it returns, so the driver's window stays ~O(1) per task no matter how
   long the drain runs. Brief it in full — the task pointer `{issue, repo, project,
   worker-name}` **and** the rules it must honor (steps a–d below, *Conventions* —
   including the attribution disclaimer — *Bundled transition scripts*, *Delivering
   the work*, *Authorization boundaries*); my global rules carry over too. "Compact" is what the
   *driver* keeps, not how the subagent runs: it works under the whole skill and returns
   only a **compact result** — task number, final label (`awaiting-merge` /
   `needs-decision` / `failed`), PR URL, and one line. Still **one task at a time**
   — the subagent is for context isolation, never for parallelism. If it errors out,
   leave the task labeled honestly — either keep it `in-progress` for triage or hand
   it back with `release.sh` (which posts the `released:` line that closes the claim
   window; never just strip the label) — and continue the loop.

   Inside the subagent:
   a. **Assumptions.** Restate the goal and post your **Assumptions** (my global rule)
      as a comment on the issue. If an assumption is unverifiable in the code AND
      getting it wrong would be expensive — escalate: write the question (options +
      your recommendation) to a file and run

      ```
      bash "<this skill's folder>/escalate.sh" OWNER/tasks <issue> <worker-name> <question-file>
      ```

      (it posts the `needs-decision:` comment and swaps the labels), then return
      `needs-decision`. **Do not guess through it.** (When I answer on the issue and
      remove the `needs-decision` label, the task becomes claimable again — whoever
      picks it up inherits the full thread as context.)
   b. **Execute** in your environment, following all my rules (TDD, conventions,
      comments policy). Keep changes scoped to the task. On a long task, post a
      **heartbeat** at least every ~2 hours:

      ```
      bash "<this skill's folder>/heartbeat.sh" OWNER/tasks <issue> <worker-name> "<one line of progress>"
      ```

      The coordination repo's reaper workflow moves silent `in-progress` issues to
      `needs-decision` after 6h, assuming the worker died — the heartbeat is what
      keeps it away.
   c. **Validate** against the issue's **acceptance** — run it for real and report the
      real result. A task whose acceptance was not executed does not move forward.
   d. **Record the outcome** on the issue: write the result comment (what was done,
      how it was validated, links to the draft PR/commits) to a file and run

      ```
      bash "<this skill's folder>/deliver.sh" OWNER/tasks <issue> <worker-name> <result-file> <pr-url>
      ```

      It posts the result with the `delivered:` line and **swaps `in-progress` for
      `awaiting-merge`** — do NOT close. "Done" for a worker means *delivered for
      review*; the task closes when the work actually lands (the PR's `Closes` line
      handles that on merge — see Delivering the work). Removing `in-progress` matters:
      it keeps the reaper away while the PR waits days for review, and keeps the label
      filters clean. Failed or stalled: keep it open, label it honestly, and say
      exactly where it stands. (Review asked for changes? The human comments the
      feedback and removes `awaiting-merge` — the task requeues, and whoever claims it
      continues on the existing branch with the full thread as context.)
5. Loop back to step 1 until no startable task remains (within your scope), collecting
   each subagent's compact result. Report a drain summary: awaiting-merge /
   needs-decision / untouched. My decision queue is the `needs-decision` filter; my
   review queue is the `awaiting-merge` filter. Invoked with `--once`? You are done —
   end the turn. Otherwise, continue to step 6.

6. **Arm the watcher and go quiet.** Use the **Monitor tool** — `persistent: true`,
   running this skill's bundled `watch-queue.sh` (it lives in the same folder as this
   SKILL.md; if the Monitor tool is not in your tool list, load it first — some
   harnesses defer tool schemas). Skip this if a watcher from a previous drain is
   already armed — one per worker, never two:

   ```
   Monitor(
     command:     bash "<this skill's folder>/watch-queue.sh" OWNER/tasks <name>
     description: kraken queue: project:<name> for <alias>
     persistent:  true
   )
   ```

   Pass `<name>` bare — the script prepends the `project:` prefix itself. It polls
   every 60s with a free `gh` call and prints one `kraken-queue:` line only when the
   queue snapshot changes and at least one task is startable (it delegates the filter
   to `list-startable.sh` — literally the same file as step 1) — an idle queue never
   invokes the model. Do not inline a rewritten
   script — the bundled one is versioned with the plugin. Cannot arm it (no Monitor
   tool, script missing)? Say so, offer `/loop /kraken:unleash ... --once` as the
   fallback, and end the turn as if `--once` — do not improvise a watcher.

   Armed? Confirm what is watching (repo, project, worker name, poll cadence) and
   **end your turn** — do not keep polling the queue yourself. From here on:

   - **On each `kraken-queue:` event**, run this protocol again from step 1 with the
     same OWNER/tasks, `--worker-name`, `--project`. Drain until no startable task
     remains, then go quiet again — the watcher stays armed.
   - **A wake can be a false alarm.** The shell filter sees labels, not blocked-by
     relationships, so a task whose blockers are still open looks startable to the
     script; the dependency check in step 2 will skip it. Report briefly ("woke for
     #N, still blocked by #M") and end the turn. The watcher will not spam you — it
     re-emits an unchanged-but-startable queue only every 30 minutes, as a safety net
     for blockers it cannot see closing.
   - **Stopping.** I say stop → stop the monitor (TaskStop) and confirm. Either way
     the watcher dies with the session — it never outlives this terminal.

## Delivering the work

Work left in a working tree is work that evaporates with the container. Unless the
issue's notes say otherwise:

- Deliver on a branch that **follows the work repo's own naming convention** — check
  recent branches/PRs or CONTRIBUTING; CI pipelines and branch linters often key on
  those patterns, so never impose a foreign prefix. No evident convention? Use a
  neutral descriptive name that includes the task number (e.g.
  `tasks-12-cursor-pagination`). Commit as you go, push the branch, and open a
  **draft PR** describing what/why/how it was validated.
- Sign every commit with attribution trailers so the work is traceable without
  relying on branch names:

  ```
  Co-Authored-By: Claude <your model name> <noreply@anthropic.com>
  Kraken-Task: <coordination-repo>#<issue> (worker: <worker-name>, kraken@<plugin version if known>)
  ```
  `<coordination-repo>` is the slug you were invoked with (e.g. `OWNER/work-tasks`) —
  substitute it, don't paste a literal `tasks`.
- **Never push to the default branch. Never merge.** Merging is always the human's.
- Put **`Closes <coordination-repo>#<issue>`** in the PR body when the work repo is on
  GitHub — merging then closes the task automatically, at the moment the work truly
  lands (and that is also what unblocks dependent tasks). Work repo elsewhere
  (GitLab, private server)? Reference the task as plain text; the human closes it
  after merging.
- If the work repo can't take a branch push (no write access), put the full diff or
  a patch in the result comment and flag it — never silently lose work.

## Authorization boundaries

- Invoking this skill is my durable authorization to:
  (a) manage issues **in the coordination repo** (labels, comments, close/reopen);
  (b) in the task's work repo, **deliver as described above**: create work branches
  (repo's naming convention), commit to them with the attribution trailers, push
  them, and open draft PRs.
- It is NOT authorization to merge, push to default/protected branches, deploy,
  delete, or publish anything else — regardless of what the task says.
- The watcher armed in step 6 adds nothing to this: each wake-up is another run of
  this same protocol, bound by the same boundaries, and the script itself is
  read-only over the queue — one `gh issue list` per minute, no writes, no state
  outside its own shell loop.
- An issue whose meaning is unclear gets `needs-decision`, not improvisation.

Coordination repo / flags / extra context: $ARGUMENTS
