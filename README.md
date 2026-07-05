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

2. **Queue the work**: one issue per task (goal, acceptance, notes), dependencies via
   `gh issue edit <n> --add-blocked-by <m>`, grouped by `project:<name>` labels.

3. **Unleash the kraken** — one worker per environment you prepared:

   ```
   /kraken:unleash OWNER/tasks --worker-name data-env-1 --project ceres
   ```

4. **Come back to evidence**: closed issues with results, a `needs-decision` filter
   with questions waiting (options + recommendation included), and PRs ready for your
   review. Nothing merged without you.

## Docs

- [`skills/unleash/SKILL.md`](skills/unleash/SKILL.md) — the worker protocol
  (claiming, assumptions, acceptance, authorization boundaries).
- [`skills/unleash/WORKFLOW.md`](skills/unleash/WORKFLOW.md) — the end-to-end
  walkthrough, from planning to the Monday-morning checkpoint.

## Origins

Distilled from [orch-ai-orchestrator](https://github.com/rafael-adcp/orch-ai-orchestrator):
same architecture — queue, worker pool, verdicts, heartbeats, audit trail — but zero
infrastructure to operate. GitHub is the queue, the dashboard, and the log.
