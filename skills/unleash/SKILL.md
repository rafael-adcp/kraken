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
If the `OWNER/tasks` slug matches `^OWNER/` or contains `<`/`>`, refuse: it looks like
the template placeholder — substitute your real `owner/repo` and re-run.

- `--worker-name`: this worker's identity, used in every claim/comment. Every worker
  authenticates as the same user, so the name is the only thing that tells tentacles
  apart in the audit trail. Pick names that say where the work ran.
- `--project`: only take tasks labeled `project:<name>`. Mandatory because a worker
  runs in an environment prepared for a specific project — an unscoped worker could
  claim a task its environment cannot host.

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
  carry their attribution in the `Kraken-Task` / `Co-Authored-By` trailers.
- A task is an **open issue labeled `kraken-task`**, created from the repo's task
  template — **goal** (the outcome to plan toward, restated as Assumptions),
  **acceptance** (the executable proof to run at validation time), and optional
  **notes** (constraints, frozen contracts, gotchas).
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
3. **Claim**. Your candidate list may be stale — another worker may have delivered or
   released the task in the seconds since you listed. So **re-fetch the issue's current
   labels first** and skip it if it now carries `in-progress`, `needs-decision`, or
   `awaiting-merge`: never add `in-progress` on top of one of those, or you corrupt the
   state (`in-progress` + `awaiting-merge` is exactly what the reaper later drags to
   `needs-decision`). Check before you label — do not label-then-verify. Only if it is
   still clear, claim: add the `in-progress` label and immediately post a comment
   `claimed-by: <worker-name>`. Then **re-read the comments**: if the first
   `claimed-by:` comment **of the current claim window** is not yours, another
   worker won — back off (remove nothing) and pick the next candidate. Comment
   ordering is server-side, which makes it the tiebreaker: the re-fetch guards against a
   stale list, the tiebreaker resolves two workers that pass the guard at the same
   instant. (Assignees can't arbitrate: every worker authenticates as me.)
   The claim window starts after the most recent `released:` / stale-claim /
   `needs-decision` event on the issue — **ignore `claimed-by:` comments older than
   that**, or a task once claimed by a dead worker could never be claimed again.
4. **Hand the claimed task to a fresh subagent.** Everything from here to delivery is
   the heavy part — reading and writing many files — and ~90% of it stops mattering the
   moment the task ships. Run it in a **new subagent**: a context born empty and
   discarded when it returns, so the driver's window stays ~O(1) per task no matter how
   long the drain runs. Brief it in full — the task pointer `{issue, repo, project,
   worker-name}` **and** the rules it must honor (steps a–d below, *Conventions* —
   including the attribution disclaimer — *Delivering the work*, *Authorization
   boundaries*); my global rules carry over too. "Compact" is what the
   *driver* keeps, not how the subagent runs: it works under the whole skill and returns
   only a **compact result** — task number, final label (`awaiting-merge` /
   `needs-decision` / `failed`), PR URL, and one line. Still **one task at a time**
   — the subagent is for context isolation, never for parallelism. If it errors out,
   leave the task labeled honestly (never silently drop an `in-progress` claim) and
   continue the loop.

   Inside the subagent:
   a. **Assumptions.** Restate the goal and post your **Assumptions** (my global rule)
      as a comment on the issue. If an assumption is unverifiable in the code AND
      getting it wrong would be expensive — swap `in-progress` for `needs-decision`,
      comment the question with options + your recommendation, and return
      `needs-decision`. **Do not guess through it.** (When I answer on the issue and
      remove the `needs-decision` label, the task becomes claimable again — whoever
      picks it up inherits the full thread as context.)
   b. **Execute** in your environment, following all my rules (TDD, conventions,
      comments policy). Keep changes scoped to the task. On a long task, post a short
      progress comment at least every ~2 hours — it is your **heartbeat**: the
      coordination repo's reaper workflow moves silent `in-progress` issues to
      `needs-decision` after 6h, assuming the worker died.
   c. **Validate** against the issue's **acceptance** — run it for real and report the
      real result. A task whose acceptance was not executed does not move forward.
   d. **Record the outcome** on the issue: a result comment (what was done, how it was
      validated, links to the draft PR/commits), then **swap `in-progress` for
      `awaiting-merge`** — do NOT close. "Done" for a worker means *delivered for
      review*; the task closes when the work actually lands (the PR's `Closes` line
      handles that on merge — see Delivering the work). Removing `in-progress` matters:
      it keeps the reaper away while the PR waits days for review, and keeps the label
      filters clean. Failed or stalled: keep it open, label it honestly, and say
      exactly where it stands. (Review asked for changes? The human comments the
      feedback and removes `awaiting-merge` — the task requeues, and whoever claims it
      continues on the existing branch with the full thread as context.)
5. Loop back to step 1 until no startable task remains (within your scope), collecting
   each subagent's compact result. Finish with a summary: awaiting-merge /
   needs-decision / untouched. My decision queue is the `needs-decision` filter; my
   review queue is the `awaiting-merge` filter.

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
- An issue whose meaning is unclear gets `needs-decision`, not improvisation.

Coordination repo / flags / extra context: $ARGUMENTS
