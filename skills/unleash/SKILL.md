---
name: unleash
description: Run as a named worker draining the task queue in my private coordination repo, where each task is a GitHub Issue — claim one task at a time, check blocked-by dependencies, plan with explicit assumptions, execute, validate against acceptance criteria, and record results as comments. The async/weekend orchestration driver.
---

# Kraken — one head, many tentacles

You are a **tentacle**: a named worker draining the task queue in the kraken's head —
my **coordination repo**, a private repo whose GitHub Issues ARE the tasks. Work repos
can live anywhere (GitHub, GitLab, private servers) — each issue says which project it
belongs to. GitHub's UI/CLI does the tracking: status, history, notifications, and the
dependency graph come for free.

## Invocation

```
/kraken:unleash OWNER/tasks --worker-name <alias> --project <name>
```

**All three arguments are REQUIRED.** If any is missing, do not start — ask for it.

- `--worker-name`: this worker's identity, used in every claim/comment. Every worker
  authenticates as the same user, so the name is the only thing that tells tentacles
  apart in the audit trail. Pick names that say where the work ran.
- `--project`: only take tasks labeled `project:<name>`. Mandatory because a worker
  runs in an environment prepared for a specific project — an unscoped worker could
  claim a task its environment cannot host.

## The concurrency model: capacity = how many workers I launch

- **You work ONE task at a time.** Never claim a second task before finishing (or
  releasing) the first. Subagents are fine *within* a task, never to run extra tasks.
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
- A task is an **open issue labeled `kraken-task`**, created from the repo's task
  template (fields: goal, acceptance, notes).
- Every task carries a **`project:<name>`** label — that's what `--project` filters
  on, and it is the project's **canonical identity**: the worker runs in an
  environment prepared for that project, so the label (not a repo path baked into the
  task) is what says where the work lands. A task without a project label is invisible
  to every worker (fix the label, don't improvise).
- Setting up a new coordination repo? Copy the assets bundled in this skill's
  folder: `task-template.yml` → `.github/ISSUE_TEMPLATE/task.yml` and
  `reclaim-stale.yml` → `.github/workflows/reclaim-stale.yml` (the reaper for dead
  workers' claims); create the labels `kraken-task`, `in-progress`, `needs-decision`,
  `awaiting-merge`.

## Protocol

1. List candidates: open `kraken-task` issues scoped to your project, **without
   `in-progress`, `needs-decision`, or `awaiting-merge`** (those are running or
   waiting on a human — never claim one), oldest first. Filter labels client-side —
   it's deterministic, while mixing `--label` with `--search` in `gh` is not:

   ```
   gh -R OWNER/REPO issue list --state open --limit 100 \
     --label kraken-task --label "project:<name>" \
     --json number,title,labels,createdAt \
     --jq '[.[] | select([.labels[].name] | (index("in-progress") or index("needs-decision") or index("awaiting-merge") | not))] | sort_by(.createdAt)'
   ```
2. Skip anything blocked: a task is startable only when **every blocked-by issue is
   closed** (check the issue's relationships; honor a `depends-on: #N` line in the
   body as a fallback for the same thing).
3. **Claim**: add the `in-progress` label and immediately post a comment
   `claimed-by: <worker-name>`. Then **re-read the comments**: if the first
   `claimed-by:` comment **of the current claim window** is not yours, another
   worker won — back off (remove nothing) and pick the next candidate. Comment
   ordering is server-side, which makes it the tiebreaker. (Assignees can't
   arbitrate: every worker authenticates as me.)
   The claim window starts after the most recent `released:` / stale-claim /
   `needs-decision` event on the issue — **ignore `claimed-by:` comments older than
   that**, or a task once claimed by a dead worker could never be claimed again.
4. Before touching code: restate the goal and post your **Assumptions** (my global
   rule) as a comment on the issue. If an assumption is unverifiable in the code AND
   getting it wrong would be expensive — swap `in-progress` for `needs-decision`,
   comment the question with options + your recommendation, and move on to the next
   task. **Do not guess through it.** (When I answer on the issue and remove the
   `needs-decision` label, the task becomes claimable again — whoever picks it up
   inherits the full thread as context.)
5. Execute in your environment, following all my rules (TDD, conventions, comments
   policy). Keep changes scoped to the task. On a long task, post a short progress
   comment at least every ~2 hours — it is your **heartbeat**: the coordination
   repo's reaper workflow moves silent `in-progress` issues to `needs-decision`
   after 6h, assuming the worker died.
6. Validate against the issue's **acceptance** — run it for real and report the real
   result. A task whose acceptance was not executed does not move forward.
7. Record the outcome on the issue: a result comment (what was done, how it was
   validated, links to the draft PR/commits), then **swap `in-progress` for
   `awaiting-merge`** — do NOT close. "Done" for a worker means *delivered for
   review*; the task closes when the work actually lands (the PR's `Closes` line
   handles that on merge — see Delivering the work). Removing `in-progress` matters:
   it keeps the reaper away while the PR waits days for review, and keeps the label
   filters clean. Failed or stalled: keep it open, label it honestly, and say
   exactly where it stands.
   (Review asked for changes? The human comments the feedback and removes
   `awaiting-merge` — the task requeues, and whoever claims it continues on the
   existing branch with the full thread as context.)
8. Loop back to step 1 until no startable task remains (within your scope). Finish
   with a summary: awaiting-merge / needs-decision / untouched. My decision queue is
   the `needs-decision` filter; my review queue is the `awaiting-merge` filter.

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
  Kraken-Task: OWNER/tasks#<issue> (worker: <worker-name>, kraken@<plugin version if known>)
  ```
- **Never push to the default branch. Never merge.** Merging is always the human's.
- Put **`Closes OWNER/tasks#<issue>`** in the PR body when the work repo is on
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
- An issue whose meaning is unclear gets `needs-decision`, not improvisation.

Coordination repo / flags / extra context: $ARGUMENTS
