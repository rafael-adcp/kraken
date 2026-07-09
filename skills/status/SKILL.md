---
name: status
description: Read-only status of a kraken queue — the return-side companion to identify. Surface the review queue (awaiting-merge, with PR links), the decision queue (needs-decision), and what is still in-progress (worker name + heartbeat age), and flag a possible orphan whose PR looks merged but whose issue never closed. No writes, no label changes.
---

# Kraken — surface the depths

You are the return step for kraken: `identify` helps me *launch*, `status` helps me
*land*. Given a coordination repo, you print what needs me — the review queue and the
decision queue — plus what is still in flight, and you flag the one blind spot in the
state machine: an `awaiting-merge` task whose PR is already merged but whose issue was
never closed (the reaper only watches `in-progress`, so these can sit forever). You read
the queue; you never touch it.

## Invocation

```
/kraken:status OWNER/tasks [--project <name>]
```

The `OWNER/tasks` argument is REQUIRED — the coordination repo whose queue you report
on. Missing? Do not guess. Ask for it and stop.

If the slug matches `^OWNER/` or contains `<`/`>`, refuse: it looks like the template
placeholder — substitute your real `owner/repo` and re-run.

`--project <name>` is OPTIONAL here (unlike `unleash` / `watch`). A worker needs project
scoping because it runs in a prepared environment; a human checking the queue does not.
With `--project`, filter every list to `project:<name>`; without it, report across the
whole queue (group by `project:` label when it clarifies the picture).

## Protocol

1. **Base filters — one `gh issue list` per label.** Add `--label "project:<name>"`
   to each only when `--project` was passed:

   ```
   gh -R OWNER/tasks issue list --state open --label awaiting-merge \
     --json number,title,labels,comments,updatedAt
   gh -R OWNER/tasks issue list --state open --label needs-decision \
     --json number,title,labels,updatedAt
   gh -R OWNER/tasks issue list --state open --label in-progress \
     --json number,title,labels,comments,updatedAt
   ```

2. **Review queue (`awaiting-merge`).** For each, find the PR/MR link — parse it from
   the result comment (the worker records the draft PR there) or from a linked PR. Print
   number, title, and the link.

3. **Decision queue (`needs-decision`).** For each, print number and title; the options
   live in the issue thread (note that, don't re-summarize them).

4. **In flight (`in-progress`).** For each, print number, title, the worker name (from
   the `claimed-by: <worker-name>` comment), and the **heartbeat age** — time since the
   issue's most recent comment. A large age is a worker that may have died (the reaper
   moves silent `in-progress` issues to `needs-decision` after 6h).

5. **Possible-orphan flag (cheap heuristic, flag-only).** For each `awaiting-merge`
   issue, parse the PR URL from its result comment and check the PR's state:

   ```
   gh pr view <pr-url> --json state,mergedAt
   ```

   If the PR is already merged but the issue is still open, flag it: its work landed but
   the issue never closed (a `Closes` reference missing, or a non-GitHub work repo). This
   is a heuristic — it **flags, it never acts**. It changes no labels and closes no
   issues; the operator decides.

6. **Print the grouped summary.** Read-only from here — nothing is written. Output
   sketch (drop the `project:<name>` suffix when no `--project` was passed):

   ```
   🐙 kraken status — project:<name> @ OWNER/tasks

     📋 Review queue (awaiting-merge) — N waiting for your merge
        #88  <title>     → PR/MR link
        #91  <title>     → PR/MR link

     ❓ Decision queue (needs-decision) — N waiting for your call
        #97  <title>     (options in thread)

     ⚙️  In flight (in-progress) — N running
        #99  <title>  · worker <name> · last heartbeat 12m ago

     ⚠️  1 possible orphan: #85 awaiting-merge 4d — its PR looks merged, close it?
   ```

   Empty groups: say so plainly (e.g. "Review queue — nothing waiting").

## Authorization boundaries

- Read-only. This skill runs `gh issue list` / `gh pr view` calls and prints a summary.
- It does NOT write, comment, change labels, close issues, or merge anything — not even
  the orphan it flags. Every action is mine; the output tells me what needs me.

Coordination repo / flags / extra context: $ARGUMENTS
