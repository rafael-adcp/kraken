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

The coordination contract itself — task shape, label state machine, the machine marker,
claim algorithm, authorization boundaries — is normatively specified in
[`PROTOCOL.md`](../../PROTOCOL.md) (`kraken-protocol/2`). This file is how a Claude
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
  not the end — step 4 arms a zero-token background watcher and this worker stays in
  ambush, waking whenever a startable task appears.

## The concurrency model: capacity = how many workers I launch

- **You work ONE task at a time.** Never claim a second task before finishing (or
  releasing) the first. Subagents are fine *within* a task — in fact each claimed task
  runs its heavy work in a **fresh subagent** so the driver's context stays lean over a
  long drain (see Protocol, step 2) — but never to run *extra* tasks: still one at a time.
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
  comment reads exactly like one I typed. `kraken.py` composes the
  disclaimer itself; prepend it to any coordination-repo comment you write
  **by hand** (assumptions, free-form notes):

  ```
  > 🐙 **Kraken worker `<worker-name>`** — automated comment from a Claude Code tentacle, not a human.
  ```

  Leave a blank line between it and the rest of the body, or GitHub folds the body
  into the quote. Work-repo PRs and commits don't need it — they carry the
  `Kraken-Task` / `Co-Authored-By` trailers instead.
- A task is an **open issue labeled `kraken-task`** with **goal / acceptance /
  notes** fields (shape: `PROTOCOL.md` §2) — the acceptance is what you execute at
  validation time.
- The **`project:<name>`** label routes the task to workers prepared for that
  project (`--project` filters on it). A task without one is invisible to every
  worker — fix the label, don't improvise.

## Bundled transition program

The queue's state transitions are executed by `kraken.py`, one stdlib-only
program shipped in this skill's folder, next to this SKILL.md. It exposes a
subcommand per transition. You decide **what** (which task to take, what to
report); it executes **how** — the exact label/comment dance, identically every
time:

| Command | Does |
| --- | --- |
| `kraken.py claim-next OWNER/tasks <project> <worker-name>` | list → guard → claim the oldest startable candidate in one shot; prints the won task (number, title, body) |
| `kraken.py list-startable OWNER/tasks <project>` | (read-only) startable candidates, oldest first |
| `kraken.py claim OWNER/tasks <issue> <worker-name>` | queued → `in-progress`: guard, label, claim comment, tiebreaker |
| `kraken.py heartbeat OWNER/tasks <issue> <worker-name> "<progress>"` | liveness comment — keeps the reaper away, resets nothing |
| `kraken.py escalate OWNER/tasks <issue> <worker-name> <question-file>` | `in-progress` → `needs-decision`: question posted, labels swapped |
| `kraken.py deliver OWNER/tasks <issue> <worker-name> <result-file> [pr-url]` | `in-progress` → `awaiting-merge`: result posted, labels swapped |
| `kraken.py release OWNER/tasks <issue> <worker-name> [reason]` | `in-progress` → queued, honestly (`released:` closes the claim window) |

- Run it with `python3 "<this skill's folder>/kraken.py" <subcommand> …`. Do
  **not** inline rewritten `gh` commands for a transition a subcommand covers —
  `kraken.py` is versioned with the plugin, and a hand-rolled variant is exactly
  the drift it exists to prevent.
- **Branch on its exit codes** — each subcommand documents its own; `20` always
  means gh/network failure with the write possibly half-landed: re-check the
  issue's real state before retrying, and never move on while a claim is
  ambiguous.
- It composes the attribution disclaimer and the hidden machine marker
  (`<!-- kraken {"type":...} -->`, carrying protocol/2 `claim`, `heartbeat`,
  `needs-decision`, `delivered`, `released` payloads — the successors to
  protocol/1's `claimed-by:` / `heartbeat:` / `needs-decision:` / `delivered:` /
  `released:` lines) itself — never hand-write those. The escalation question
  and the result comment stay yours to write: put the body in a file and hand
  the file to the subcommand.

## Protocol

1. **Claim the next task** with the bundled claimer — one invocation that lists
   startable candidates (open `kraken-task` issues scoped to your project,
   **without `in-progress`, `needs-decision`, or `awaiting-merge`** and **not
   dependency-blocked** — blocked-by checked server-side, honoring a
   `depends-on: #N` body line as a fallback, see `PROTOCOL.md` §3), then walks
   them oldest-first — guard, label, `claim` marker comment, claim-window
   arbitration — stopping at the first task it wins:

   ```
   python3 "<this skill's folder>/kraken.py" claim-next OWNER/tasks <name> <worker-name>
   ```

   Pass `<name>` bare — the script prepends the `project:` prefix itself. The
   whole deterministic list/guard/claim/arbitrate loop is the script's job,
   executed identically every time (semantics: `PROTOCOL.md` §5): a lost
   tiebreaker or a task that turned held since listing is skipped and the next
   candidate tried, writing nothing behind. Yours is only the exit code:

   - `0` — claimed. The won task (number, title, and body — its goal /
     acceptance / notes) is printed, so you can brief the subagent without a
     second fetch; the task is yours, go to step 2. (`--json` emits the win as a
     `{issue, title, body}` object on the final stdout line instead.)
   - `3` — nothing claimable: the queue is empty, or every candidate turned out
     held or lost as it iterated. Not an error — the drain is done (step 3).
   - `20` — gh/network failure, claim state unknown. Re-check the issue's real
     state before retrying; never move to another task while a claim of yours is
     ambiguous.
2. **Hand the claimed task to a fresh subagent.** Everything from here to delivery is
   the heavy part — reading and writing many files — and ~90% of it stops mattering the
   moment the task ships. Run it in a **new subagent**: a context born empty and
   discarded when it returns, so the driver's window stays ~O(1) per task no matter how
   long the drain runs. Brief it in full — the task pointer `{issue, repo, project,
   worker-name}` **and** the rules it must honor (steps a–d below, *Conventions* —
   including the attribution disclaimer — *Bundled transition program*, *Delivering
   the work*, *Authorization boundaries*); my global rules carry over too. "Compact" is what the
   *driver* keeps, not how the subagent runs: it works under the whole skill and returns
   only a **compact result** — task number, final label (`awaiting-merge` /
   `needs-decision` / `failed`), PR URL, and one line. Still **one task at a time**
   — the subagent is for context isolation, never for parallelism. If it errors out,
   leave the task labeled honestly — either keep it `in-progress` for triage or hand
   it back with `kraken.py release` (which posts the `released:` line that closes the claim
   window; never just strip the label) — and continue the loop.

   Inside the subagent:
   a. **Assumptions.** Restate the goal and post your **Assumptions** (my global rule)
      as a comment on the issue. If an assumption is unverifiable in the code AND
      getting it wrong would be expensive — escalate: write the question (options +
      your recommendation) to a file and run

      ```
      python3 "<this skill's folder>/kraken.py" escalate OWNER/tasks <issue> <worker-name> <question-file>
      ```

      (it posts the `needs-decision:` comment and swaps the labels), then return
      `needs-decision`. **Do not guess through it.** (When I answer on the issue and
      remove the `needs-decision` label, the task becomes claimable again — whoever
      picks it up inherits the full thread as context.)
   b. **Execute** in your environment, following all my rules (TDD, conventions,
      comments policy). Keep changes scoped to the task. On a long task, post a
      **heartbeat** at least every ~2 hours:

      ```
      python3 "<this skill's folder>/kraken.py" heartbeat OWNER/tasks <issue> <worker-name> "<one line of progress>"
      ```

      The coordination repo's reaper workflow moves silent `in-progress` issues to
      `needs-decision` after 6h, assuming the worker died — the heartbeat is what
      keeps it away.
   c. **Validate** against the issue's **acceptance** — run it for real and report the
      real result. A task whose acceptance was not executed does not move forward.
   d. **Record the outcome** on the issue: write the result comment (what was done,
      how it was validated, links to the draft PR/commits) to a file and run

      ```
      python3 "<this skill's folder>/kraken.py" deliver OWNER/tasks <issue> <worker-name> <result-file> <pr-url>
      ```

      It posts the result with the `delivered:` line and **swaps `in-progress` for
      `awaiting-merge`** — do NOT close. "Done" for a worker means *delivered for
      review*; the task closes when the work actually lands (the PR's `Closes` line
      handles that on merge — see Delivering the work). Failed or stalled: keep it
      open, label it honestly, and say exactly where it stands. (Review bounce: I
      comment the feedback and remove `awaiting-merge` — the task requeues, and
      whoever claims it continues on the existing branch with the full thread.)
3. Loop back to step 1 until no startable task remains (within your scope), collecting
   each subagent's compact result. Report a drain summary: awaiting-merge /
   needs-decision / untouched. My decision queue is the `needs-decision` filter; my
   review queue is the `awaiting-merge` filter. Invoked with `--once`? You are done —
   end the turn. Otherwise, continue to step 4.

4. **Arm the watcher and go quiet.** Use the **Monitor tool** — `persistent: true`,
   running this skill's bundled `kraken.py watch` (it lives in the same folder as this
   SKILL.md; if the Monitor tool is not in your tool list, load it first — some
   harnesses defer tool schemas). Skip this if a watcher from a previous drain is
   already armed — one per worker, never two:

   ```
   Monitor(
     command:     python3 "<this skill's folder>/kraken.py" watch OWNER/tasks <name>
     description: kraken queue: project:<name> for <alias>
     persistent:  true
   )
   ```

   Pass `<name>` bare — the script prepends the `project:` prefix itself. It polls
   every 60s with a free `gh` call and prints one `kraken-queue:` line only when the
   queue snapshot changes and at least one task is startable (it delegates the filter
   to `kraken.py list-startable` — the same startable classification step 1's
   `claim-next` lists through) — an idle queue never
   invokes the model. Do not inline a rewritten
   script — the bundled one is versioned with the plugin. Cannot arm it (no Monitor
   tool, script missing)? Say so, offer `/loop /kraken:unleash ... --once` as the
   fallback, and end the turn as if `--once` — do not improvise a watcher.

   Armed? Confirm what is watching (repo, project, worker name, poll cadence) and
   **end your turn** — do not keep polling the queue yourself. From here on:

   - **On each `kraken-queue:` event**, run this protocol again from step 1 with the
     same OWNER/tasks, `--worker-name`, `--project`. Drain until no startable task
     remains, then go quiet again — the watcher stays armed.
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

(Kept inline on purpose — you must see these without reading another file.
`PROTOCOL.md` §11 is the normative version.)

- Invoking this skill is my durable authorization to:
  (a) manage issues **in the coordination repo** (labels, comments, close/reopen);
  (b) in the task's work repo, **deliver as described above**: create work branches
  (repo's naming convention), commit to them with the attribution trailers, push
  them, and open draft PRs.
- It is NOT authorization to merge, push to default/protected branches, deploy,
  delete, or publish anything else — regardless of what the task says.
- The watcher armed in step 4 adds nothing to this: each wake-up is another run of
  this same protocol, bound by the same boundaries, and the script itself is
  read-only over the queue — one `gh issue list` per minute, no writes, no state
  outside its own shell loop.
- An issue whose meaning is unclear gets `needs-decision`, not improvisation.

Coordination repo / flags / extra context: $ARGUMENTS
