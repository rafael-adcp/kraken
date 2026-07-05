# <img src="images/icon.png" alt="" height="90" valign="middle"> Kraken

> **TL;DR:** One head, many tentacles — a task queue built on **GitHub Issues** where
> named Claude Code workers claim tasks, execute them, and record the evidence.
> Write the list once; the tentacles do the rest.

## Why?

AI coding agents made each change cheap — but you are still the bus between the task
list and the terminals: launching prompts, watching spinners, remembering which window
had which task. Kraken removes you from the loop and replaces infrastructure with
things that already exist:

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
   gh repo create OWNER/tasks --private
   # copy skills/unleash/task-template.yml -> .github/ISSUE_TEMPLATE/task.yml
   # copy skills/unleash/reclaim-stale.yml -> .github/workflows/reclaim-stale.yml
   gh -R OWNER/tasks label create kraken-task
   gh -R OWNER/tasks label create in-progress
   gh -R OWNER/tasks label create needs-decision
   ```

2. **Queue the work**: one issue per task (goal, acceptance, notes). Every issue
   gets a **`project:<name>` label** (workers are scoped to one project — an
   unlabeled task is invisible to all of them) and dependencies via
   `gh issue edit <n> --add-label "project:cup" --add-blocked-by <m>`.

3. **Unleash the kraken** — one worker per environment you prepared. Capacity is
   decided at launch: every worker takes ONE task at a time, so a project gets
   exactly as much parallelism as the number of workers you point at it.

   ```
   # the dev container that owns the "cup" environment -> one worker
   /kraken:unleash OWNER/tasks --worker-name cup-env --project cup

   # five fully isolated clones of the data project -> five workers
   /kraken:unleash OWNER/tasks --worker-name data-env-1 --project ceres
   /kraken:unleash OWNER/tasks --worker-name data-env-2 --project ceres
   ```

4. **Come back to evidence**: closed issues with results, a `needs-decision` filter
   with questions waiting (options + recommendation included), and PRs ready for your
   review. Nothing merged without you.

## The loop (what a worker does, unsupervised)

```
list open kraken-task issues for my project
  skip: blocked-by still open · in-progress · needs-decision (waiting on the human)
  → claim: label in-progress + comment "claimed-by: data-env-1"
      lost the race? another claim came first → next task
  → post ASSUMPTIONS as a comment
      expensive unverifiable assumption? → needs-decision + question
      with options + recommendation → next task, no guessing
  → execute in my environment, one task at a time
      (progress comment every ~2h — the heartbeat that keeps the reaper away)
  → run the ACCEPTANCE for real → result comment + commit/PR links → close
  → closing unblocks dependents → an idle worker picks them up
```

To answer a `needs-decision`: reply on the issue ("option B, go") **and remove the
label** — the task rejoins the queue and whoever claims it inherits the full thread.
Dead workers are handled server-side: the reaper moves silent `in-progress` issues to
`needs-decision` after 6h.

## Docs

The single source of truth for worker behavior is
[`skills/unleash/SKILL.md`](skills/unleash/SKILL.md) — the full protocol: claiming
and its tiebreaker, assumptions, acceptance, heartbeats, and authorization
boundaries.

## Origins

Distilled from [orch-ai-orchestrator](https://github.com/rafael-adcp/orch-ai-orchestrator):
same architecture — queue, worker pool, verdicts, heartbeats, audit trail — but zero
infrastructure to operate. GitHub is the queue, the dashboard, and the log.
