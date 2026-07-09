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

Three skills ship in the box:

| Skill              | Role                                                                                     |
| ------------------ | ---------------------------------------------------------------------------------------- |
| `/kraken:unleash`  | The worker — claims one task at a time, executes, validates, delivers a draft PR          |
| `/kraken:watch`    | The driver — zero-token watcher that wakes a worker whenever a startable task appears     |
| `/kraken:identify` | The recon — lists a queue's `project:` labels and prints ready-to-paste `unleash` lines   |

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

3. **Prepare the worker environments** — one per worker: a machine, container,
   or just a separate clone where that worker will live, with the project's
   toolchain installed, `gh` authenticated, and git configured. Workers run
   unattended, so the environment's Claude Code settings must pre-allow the
   delivery commands — a permission prompt with nobody around stalls the task.
   In the working directory's `.claude/settings.json`:

   ```json
   {
     "permissions": {
       "allow": [
         "Bash(git add:*)",
         "Bash(git commit:*)",
         "Bash(git checkout:*)",
         "Bash(git push:*)",
         "Bash(gh:*)"
       ]
     }
   }
   ```

   Extend the list with what the project's acceptance checks need (test runner,
   package manager). Merges never need pre-allowing — they always stay with you.
   Workers that would share test state (database, fixtures, ports) cannot share
   an environment: fully isolated environments, or one worker.

4. **Unleash the kraken** — one worker per environment you prepared. Capacity is
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

5. **Come back to evidence**: an `awaiting-merge` filter = your review queue (each
   task with a result comment and a draft PR), a `needs-decision` filter = your
   decision queue (questions with options + recommendation included). Merging a PR
   closes its task (`Closes` reference) and unblocks the dependents. Nothing merges
   without you.

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

## Q&A

**A task landed in `needs-decision` — what do I do?**
Reply on the issue ("option B, go") **and remove the label** — the task rejoins
the queue, and whoever claims it inherits the full thread as context.

**A review asked for changes — how does the task go back?**
Same gesture: comment the feedback and remove `awaiting-merge`. The next claim
continues on the existing branch with the whole discussion in hand.

**A worker died mid-task — is the queue stuck?**
No. Workers heartbeat with progress comments, and the coordination repo's reaper
workflow drags any `in-progress` issue that has been silent for 6h to
`needs-decision` for you to triage — relaunch or investigate.

**Who can command my workers?**
Anyone who can open issues in the coordination repo: a task is, in effect,
instructions that will execute in your worker's environment with your
credentials. Keep the repo private, keep write access yours, and remember that
task bodies are untrusted input to an agent that can push branches.

**Does anything survive closing the terminal?**
The queue does — it's GitHub Issues. The worker doesn't: `/kraken:watch` and
`/loop` both live inside a Claude Code session. Headless drivers (system cron,
GitHub Actions) are the natural next step — see the alternatives table in
[#32](https://github.com/rafael-adcp/kraken/issues/32).

**How do I update the plugin?**
`/plugin marketplace update kraken`. The plugin is pinned to the version in its
manifest — pushes to `main` reach nobody until a release bumps it, so what you
run is always a deliberate release.

## Origins

Distilled from [orch-ai-orchestrator](https://github.com/rafael-adcp/orch-ai-orchestrator):
same architecture — queue, worker pool, verdicts, heartbeats, audit trail — but zero
infrastructure to operate. GitHub is the queue, the dashboard, and the log.
