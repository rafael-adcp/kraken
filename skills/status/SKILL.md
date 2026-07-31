---
name: status
description: Read-only console of a kraken queue — everything the operator reads in one place. Runs `kraken.py status`, which computes the review queue (awaiting-merge, with PR links), the decision queue (needs-decision), what is still in flight (every live claim ref — worker name + lease age, flagging expired leases), a merged-PR-but-open-issue orphan flag, and the launch recon (project: labels), then renders that output. No writes, no label changes.
---

# Kraken — surface the depths

You are the operator's console for kraken: the one read-only view of a coordination
repo. The whole computation lives in `kraken.py status` — the review queue and the
decision queue (what needs me), what is still in flight (and which of those claims
have gone silent), the one blind spot the state machine has (an `awaiting-merge` task
whose PR is already merged but whose issue was never closed — the reconcile only watches
claim refs, and a delivered task has none, so these can sit forever), the queue-entry hygiene report (tasks no
worker can start as filed), and the launch recon (which projects live in this queue). You run the subcommand and render
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
fetch, the PR-link parse, the lease-age anchor, the orphan heuristic, and the
project enumeration. Your job is to run it and render the result; there is no `gh`
orchestration left to do by hand.

1. **Run the subcommand.** Pass `--project <name>` through only if I gave it:

   ```
   skills/unleash/kraken.py status OWNER/tasks [--project <name>]
   ```

   It prints a ready-to-render human console. For a machine-readable snapshot
   (scripts, cron, a future `stats` over the timeline) add `--json` — see the schema
   below. The subcommand is **read-only**: it runs `gh` reads only (the batched
   GraphQL queue walk, the claim refs plus their commit meta for in-flight
   worker/lease-age, the paginated comment history for the awaiting-merge PR
   link, `gh pr view` for the orphan check, `gh label list` for the recon) and
   never writes.

2. **Render its output.** The default output already matches the summary shape below —
   surface it as-is. Its exit code is `0` on success or `20` on a `gh`/network
   transport failure (state unknown; re-run before trusting a stale view).

   ```
   🐙 kraken status — project:<name> @ OWNER/tasks

     📋 Review queue (awaiting-merge) — N waiting for your merge
        #88  <title> → PR/MR link
        #91  <title> → PR/MR link  (legacy: read from free text, no delivered marker)
        #94  <title> → review on the thread (no PR)

     ❓ Decision queue (needs-decision) — N waiting for your call
        #97  <title>  (options in thread)

     ⚙️  In flight (in-progress) — N running
        #99  <title>  · worker <name> · lease renewed 12m ago
        #93  <title>  · worker <name> · lease renewed 9h ago  ⚠️  lease expired — the next drain steals it

     ⚠️  1 possible orphan(s): #85 — PR looks merged but the issue is still open. You decide.

     🧹 Queue hygiene — N task(s) a worker cannot start as filed
        #12  <title>  · missing: project label
        A task with no project: label is invisible to every worker; one with no Goal or Acceptance stalls on arrival.

     🚀 Launch — one worker per prepared environment
        /kraken:unleash OWNER/tasks --worker-name <worker-name> --project <name-1>
        /kraken:unleash OWNER/tasks --worker-name <worker-name> --project <name-2>
   ```

   The PR link comes from the delivery's own `pr` field (PROTOCOL.md §8). The
   `legacy:` tag on a review line means no `delivered` marker carried one and the link
   was read off the thread's prose — treat it as a guess and confirm before merging.

   The lease age is anchored to the worker's **claim ref commit date** (the
   `claim`/`heartbeat` commit on the highest `refs/kraken/claims/<issue>/<gen>`),
   NOT the issue's
   `updatedAt` — an operator comment on a dead worker's issue must not make it look
   alive. The ⚠️ means the lease is past its TTL: it holds nothing any more, and the
   next worker to read the queue takes the task over (PROTOCOL.md §5). Nothing here
   acts on that — `status` writes nothing, ever. The orphan
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
  "review_queue":   [ { "number", "title", "pr_url": "<url>"|null,
                        "orphan": true|false } ],
  "decision_queue": [ { "number", "title" } ],
  "in_flight":      [ { "number", "title", "worker": "<name>"|null,
                        "heartbeat_anchor": "<iso>"|null,   // the lease timestamp
                        "heartbeat_age_seconds": <int>|null,
                        "heartbeat_msg": "<progress>"|null,
                        "stale": true|false } ],            // true = lease expired
  "orphans":  [ <number>, ... ],        // review numbers whose PR looks merged
  "projects": [ "<name>", ... ]         // every project: label (recon targets)
}
```

`heartbeat_age_seconds` / `heartbeat_anchor` / `heartbeat_msg` are `null` when there is
no readable claim ref (an orphan projection, or a claim commit that could not be read);
`stale` is `true` when the lease no longer holds — expired, or with no readable clock
behind it; `pr_url` is `null` when the delivery carried no PR — §8 records the field
"when there is one", so the diff is on the thread and the issue IS the review target,
not a delivery to go chase.

Every link here comes from the task's **state record** (PROTOCOL.md §3.1, §8), which
arrives with the queue read, so a review queue of any size costs no comment request.
Through protocol/8 the console paginated the whole thread of every delivered task
looking for the newest `delivered` marker, and reported a `pr_source` saying whether
the link was that field or a guess read off the prose. There is nothing left to
guess, so there is nothing left to report.

## Authorization boundaries

- Read-only. `kraken.py status` runs `gh` reads (`api graphql`, `git/matching-refs`
  for the claim refs, the paginated `issues/*/comments`, `pr view`, `label list`)
  and prints a summary.
- It does NOT write, comment, change labels, close issues, or merge anything — not even
  the orphan it flags. Every action is mine; the output tells me what needs me.
- It does NOT invoke `/kraken:unleash` on my behalf — the launch lines are copy-paste;
  I launch workers deliberately.

Coordination repo / flags / extra context: $ARGUMENTS
