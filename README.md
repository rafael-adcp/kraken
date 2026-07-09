# <img src="images/icon.png" alt="" height="90" valign="middle"> Kraken

[![Release](https://img.shields.io/github/v/release/rafael-adcp/kraken)](https://github.com/rafael-adcp/kraken/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

> **You set the targets; the tentacles devour them. Unleash the Kraken.**
>
> One head, many tentacles — a task queue built on **GitHub Issues** where
> named Claude Code workers claim tasks, execute them, and record the evidence.
> Write the list once; the tentacles do the rest.

## Why?

Kraken treats AI coding prompts the way a CI server treats builds: you push
them onto a queue (GitHub Issues), a pool of named workers (tentacles) picks them up, and the issue timeline tells you what happened.

AI coding agents made each change cheap — but you are still the bus between the task list and the terminals. You can only watch so many spinners, juggle so many windows, and stay awake so many hours before something gets dropped. Kraken removes you from the loop and replaces infrastructure with things that already exist:

| Concern            | Kraken's answer                                             |
| ------------------ | ----------------------------------------------------------- |
| Queue & state      | GitHub Issues in a private coordination repo you own        |
| Claiming (no race) | Label + `claimed-by` comment; server-side ordering wins     |
| Dependencies       | Native `blocked-by` relationships — closing a task unblocks |
| Parallelism        | Capacity = how many workers you launch; 1 task per worker   |
| Dead workers       | Heartbeat comments + an hourly reaper workflow              |
| Dashboard          | The GitHub UI — filters, notifications, mobile app          |
| Audit trail        | The issue timeline: who, when, why, validated how           |

Work repos can live **anywhere** (GitHub, GitLab, private servers) — only the
coordination repo needs to be on GitHub, and it holds issues, never code.

## Install (Claude Code plugin)

```
/plugin marketplace add rafael-adcp/kraken
/plugin install kraken@kraken
```

**Requirements**: `git`, and a `gh` CLI from June 2026 or later — the dependency
flags (`--add-blocked-by` / `--blocked-by`) shipped then. Older `gh` still works for
everything else; set dependencies via the Relationships sidebar instead.

## Quickstart

1. **Create the coordination repo** (once):

   ```bash
   gh repo create OWNER/tasks --private --clone && cd tasks
   mkdir -p .github/ISSUE_TEMPLATE .github/workflows
   curl -sL https://raw.githubusercontent.com/rafael-adcp/kraken/main/skills/unleash/task-template.yml -o .github/ISSUE_TEMPLATE/task.yml
   curl -sL https://raw.githubusercontent.com/rafael-adcp/kraken/main/skills/unleash/reclaim-stale.yml -o .github/workflows/reclaim-stale.yml
   git add -A && git commit -m "chore: kraken task template and reaper" && git push

   gh -R OWNER/tasks label create kraken-task
   gh -R OWNER/tasks label create in-progress
   gh -R OWNER/tasks label create needs-decision
   gh -R OWNER/tasks label create awaiting-merge
   gh -R OWNER/tasks label create "project:YOUR_PROJECT_NAME"      # one per project you'll queue
   ```

2. **Queue the work**: one issue per task (goal, acceptance, notes). Every issue
   gets a **`project:<name>` label** (workers are scoped to one project — an
   unlabeled task is invisible to all of them) and dependencies via
   `gh issue edit <n> --add-label "project:YOUR_PROJECT_NAME" --add-blocked-by <m>`.

3. **Unleash the kraken** — one worker per environment you prepared. Capacity is
   decided at launch: every worker takes ONE task at a time, so a project gets
   exactly as much parallelism as the number of workers you point at it.

   ```
   # one tentacle into the "your_project_1" environment -> one worker
   /kraken:unleash OWNER/tasks --worker-name your_project_1-env-1 --project your_project_1

   # five tentacles, five isolated clones -> five workers draining your_project_2
   /kraken:unleash OWNER/tasks --worker-name data-env-1 --project your_project_2
   /kraken:unleash OWNER/tasks --worker-name data-env-2 --project your_project_2
   ```

   Workers deliver on **work branches + draft PRs** — never the default branch,
   never a merge. Branches follow each work repo's own naming convention (CI
   pipelines key on those patterns); traceability comes from commit trailers
   (`Kraken-Task: OWNER/work-tasks#12 (worker: ..., kraken@x.y.z)`).

4. **Come back to evidence**: an `awaiting-merge` filter = your review queue (each
   task with a result comment and a draft PR), a `needs-decision` filter = your
   decision queue (questions with options + recommendation included). Merging a PR
   closes its task (`Closes` reference) and unblocks the dependents. Nothing merges
   without you.

> [!TIP]
> Forgot which projects live in the queue? Run
> `/kraken:identify OWNER/tasks` — it lists the `project:` labels and prints
> ready-to-paste `/kraken:unleash` lines, one per project.

> [!IMPORTANT]
> Workers run unattended: the worker environment's Claude Code settings must
> allow `git commit`/`git push` without prompting — a permission ask-gate with
> nobody around stalls the task at delivery time. Merges always stay with you.

## How it works (10,000 ft)

```
                     YOU
                      │ file kraken-task issues
                      ▼
    ┌────────────────────────────────────┐
    │  COORDINATION REPO (GitHub Issues) │
    │  labels · reaper · dependencies    │
    └──────────────────┬─────────────────┘
                       │ claim, heartbeat, release
                       ▼
    ┌────────────────────────────────────┐
    │  TENTACLES (Claude Code workers)   │
    │  ONE task at a time · per env      │
    └──────────────────┬─────────────────┘
                       │ push branch + draft PR
                       ▼
    ┌────────────────────────────────────┐
    │  WORK REPO (GitHub, GitLab, ...)   │
    │  draft PR with Kraken-Task trailer │
    └──────────────────┬─────────────────┘
                       │
                       ▼
                     YOU
                 (review · merge)
```

The full worker protocol — claim tiebreaker, assumptions, acceptance, heartbeats,
authorization boundaries — lives in [`skills/unleash/SKILL.md`](skills/unleash/SKILL.md).

### Answering the human queues

To answer a `needs-decision`: reply on the issue ("option B, go") **and remove the
label** — the task rejoins the queue and whoever claims it inherits the full thread.
Same gesture when a review asks for changes: comment the feedback and remove
`awaiting-merge`. Dead workers are handled server-side: the reaper moves silent
`in-progress` issues to `needs-decision` after 6h.

### Keep it draining

A single run empties the queue and stops. To keep a worker picking up tasks you
file through the day, arm the event-driven watcher:

```
/kraken:watch OWNER/tasks --worker-name your_project_env_1 --project your_project
```

A background shell script (armed via Claude Code's Monitor tool) polls the queue
every 60s with a free `gh` call and wakes the worker **only when a startable task
appears** — an idle queue costs zero LLM tokens. Each wake is an ordinary drain:
same one task at a time, same claim tiebreaker. Enqueue from anywhere (`gh issue
create`, web UI, mobile app) and the watcher picks it up within a minute; it
lives until the session closes or you say stop.

Prefer a dumb timer? `/loop 15m /kraken:unleash ...` still works — it just costs
one full LLM turn per fire even when the queue is empty.

## Witness the Depths

A real task's timeline, end to end — claim, restated goal + assumptions, the PR
delivered, the result with the acceptance check executed, and the close:

> Work happens while you don't. Queue a backlog before bed, on the commute, or before a meeting. Come back to finished branches instead of an empty editor.

<img src="images/pilot-task.png" width="720" alt="A kraken task issue timeline: claim comment, assumptions, draft PR link, result comment, and close">

## Updating

The plugin is pinned to the version in its manifest — pushes to `main` reach nobody
until a release bumps it, so what you run is always a deliberate release. To pick up
a new one:

```
/plugin marketplace update kraken
```

## Origins

Distilled from [orch-ai-orchestrator](https://github.com/rafael-adcp/orch-ai-orchestrator):
same architecture — queue, worker pool, verdicts, heartbeats, audit trail — but zero
infrastructure to operate. GitHub is the queue, the dashboard, and the log.
