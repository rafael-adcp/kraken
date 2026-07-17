---
name: status
description: Read-only console of a kraken queue — everything the operator reads in one place. Runs `kraken.py status`, which computes the review queue (awaiting-merge, with PR links), the decision queue (needs-decision), what is still in-progress (worker name + heartbeat age), a merged-PR-but-open-issue orphan flag, and the launch recon (project: labels), then renders that output. No writes, no label changes.
---

# Kraken — surface the depths

You are the operator's console for kraken: the one read-only view of a coordination
repo. The whole computation lives in `kraken.py status` — the review queue and the
decision queue (what needs me), what is still in flight, the one blind spot the state
machine has (an `awaiting-merge` task whose PR is already merged but whose issue was
never closed — the reaper only watches `in-progress`, so these can sit forever), and
the launch recon (which projects live in this queue). You run the subcommand and render
its output. You read the queue; you never touch it.

## Invocation

```
/kraken:status OWNER/tasks [--project <name>]
```

The `OWNER/tasks` argument is REQUIRED — the coordination repo whose queue you report
on. Missing? Do not guess. Ask for it and stop.

If the slug matches `^OWNER/` or contains `<`/`>`, refuse: it looks like the template
placeholder — substitute your real `owner/repo` and re-run.

`--project <name>` is OPTIONAL here (unlike `unleash`). A worker needs project
scoping because it runs in a prepared environment; a human checking the queue does not.
With `--project`, every list is scoped to `project:<name>`; without it, the whole queue
is reported and the launch recon enumerates every project.

## Protocol

Everything deterministic is mechanized in the subcommand (PROTOCOL.md §12) — the queue
fetch, the PR-link parse, the heartbeat-age anchor, the orphan heuristic, and the
project enumeration. Your job is to run it and render the result; there is no `gh`
orchestration left to do by hand.

1. **Run the subcommand.** Pass `--project <name>` through only if I gave it:

   ```
   skills/unleash/kraken.py status OWNER/tasks [--project <name>]
   ```

   It prints a ready-to-render human console. For a machine-readable snapshot
   (scripts, cron, a future `stats` over the timeline) add `--json` — see the schema
   below. The subcommand is **read-only**: it runs `gh` reads only (the batched
   GraphQL queue walk, the paginated comment history for heartbeat/PR-link, `gh pr
   view` for the orphan check, `gh label list` for the recon) and never writes.

2. **Render its output.** The default output already matches the summary shape below —
   surface it as-is. Its exit code is `0` on success or `20` on a `gh`/network
   transport failure (state unknown; re-run before trusting a stale view).

   ```
   🐙 kraken status — project:<name> @ OWNER/tasks

     📋 Review queue (awaiting-merge) — N waiting for your merge
        #88  <title> → PR/MR link
        #91  <title> → PR/MR link

     ❓ Decision queue (needs-decision) — N waiting for your call
        #97  <title>  (options in thread)

     ⚙️  In flight (in-progress) — N running
        #99  <title>  · worker <name> · last heartbeat 12m ago

     ⚠️  1 possible orphan(s): #85 — PR looks merged but the issue is still open. You decide.

     🚀 Launch — one worker per prepared environment
        /kraken:unleash OWNER/tasks --worker-name <worker-name> --project <name-1>
        /kraken:unleash OWNER/tasks --worker-name <worker-name> --project <name-2>
   ```

   The heartbeat age is anchored to the worker's last machine line
   (`^claimed-by:`/`^heartbeat:`), NOT the issue's `updatedAt` — an operator comment on
   a dead worker's issue must not make it look alive (the reaper moves silent
   `in-progress` issues to `needs-decision` after 6h off that same anchor). The orphan
   line **flags, it never acts** — no label change, no close; the decision is mine. The
   Launch section appears only without `--project`; a queue with zero `project:` labels
   says so (create one with `gh -R OWNER/tasks label create "project:<name>"` or
   `init --project`).

## `--json` schema

A stable object for downstream tooling:

```
{
  "repo": "OWNER/tasks",
  "project": "<name>" | null,           // the --project scope, or null
  "generated_at": "2026-...Z",
  "review_queue":   [ { "number", "title", "pr_url": "<url>"|null, "orphan": true|false } ],
  "decision_queue": [ { "number", "title" } ],
  "in_flight":      [ { "number", "title", "worker": "<name>"|null,
                        "heartbeat_anchor": "<iso>"|null,
                        "heartbeat_age_seconds": <int>|null } ],
  "orphans":  [ <number>, ... ],        // review numbers whose PR looks merged
  "projects": [ "<name>", ... ]         // every project: label (recon targets)
}
```

`heartbeat_age_seconds` / `heartbeat_anchor` are `null` when the worker left no machine
line (an infinitely-stale claim); `pr_url` is `null` when no PR was recorded.

## Authorization boundaries

- Read-only. `kraken.py status` runs `gh` reads (`api graphql`, the paginated
  `issues/*/comments`, `pr view`, `label list`) and prints a summary.
- It does NOT write, comment, change labels, close issues, or merge anything — not even
  the orphan it flags. Every action is mine; the output tells me what needs me.
- It does NOT invoke `/kraken:unleash` on my behalf — the launch lines are copy-paste;
  I launch workers deliberately.

Coordination repo / flags / extra context: $ARGUMENTS
