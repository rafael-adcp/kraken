# kraken — the end-to-end workflow

How the whole loop looks in practice, from planning to review. The cast: a private
**coordination repo** (`OWNER/tasks`) whose issues ARE the tasks, and one or more
named **workers** (Claude Code sessions in any container/machine, each working one
task at a time) draining the queue via `/kraken:unleash OWNER/tasks`.

## Setup (once)

```bash
gh repo create OWNER/tasks --private
# copy task-template.yml (this folder)  -> .github/ISSUE_TEMPLATE/task.yml
# copy reclaim-stale.yml (this folder)  -> .github/workflows/reclaim-stale.yml
gh -R OWNER/tasks label create kraken-task
gh -R OWNER/tasks label create in-progress
gh -R OWNER/tasks label create needs-decision
```

`reclaim-stale.yml` is the **reaper**: hourly, server-side, it moves any
`in-progress` issue silent for 6h+ to `needs-decision` — a worker died and its claim
must not block the queue forever. Workers heartbeat with progress comments, so a
healthy long task is never reaped.

Create a `project:<name>` label per project you want to group/scope by (e.g.
`project:cup`, `project:ceres`) — workers filter on these.

## 1. Plan (minutes, e.g. Friday night)

Create one issue per task from the template — UI, mobile app, or `gh`. Fuzzy idea?
Run `/rp-grill` first and paste the resulting spec into the issue. Example queue:

```
#12  Add cursor pagination to orders API     (repo: acme/api-orders)
#13  Consume paginated orders in dashboard   (repo: acme/dashboard)    blocked by #12
#14  Fix flaky auth spec                     (repo: acme/api-orders)
```

`repo` is the project's canonical identity (OWNER/REPO or clone URL) — never a local
path. Each worker runs inside an environment you prepared; the issue never cares
which clone does the work. Label the issues `project:<name>` so workers can scope to
them.

```bash
gh -R OWNER/tasks issue edit 13 --add-blocked-by 12
```

The dependency graph lives in GitHub — the execution order never needs explaining
again.

## 2. Unleash the kraken

Each tentacle (worker) is a Claude Code session started **inside an environment you
prepared** (a dev container, a clone, a machine) and named so you can tell where work
ran:

```
# in the dev container that owns the "cup" environment
/kraken:unleash OWNER/tasks --worker-name cup-vscode-env --project cup

# five fully isolated clones of the data project -> five workers
/kraken:unleash OWNER/tasks --worker-name data-env-1 --project ceres
/kraken:unleash OWNER/tasks --worker-name data-env-2 --project ceres
/kraken:unleash OWNER/tasks --worker-name data-env-3 --project ceres
...
```

**Capacity is decided at launch**: every worker takes ONE task at a time, so a
project gets exactly as much parallelism as the number of workers you point at it.
Shared test state? Launch one worker for that project. Fully isolated clones?
Launch five. The system never second-guesses you.

## 3. Each worker's loop (unsupervised)

```
list open kraken-task issues without in-progress (+ project:<name> when scoped)
  → #13 blocked (#12 open)? skip
  → #12 free → add in-progress + comment "claimed-by: data-env-1"
  → re-read comments → first claimed-by is mine? it's my task
      (someone else's? back off, try another)
  → post ASSUMPTIONS as a comment
      expensive unverifiable assumption? → needs-decision + question with options → next task
  → execute in my environment, one task at a time (TDD, rules, lint hook self-correcting)
  → run the ACCEPTANCE for real → comment result + commit/PR links → close #12
  → back to the top → #13 just unblocked itself → an idle worker picks it up
```

Note the domino: **closing #12 unblocks #13 mechanically** — no human relaying state
between terminals.

## 4. Meanwhile, you (couch / phone)

- **One place to look**: the coordination repo's issue list. Open = pending,
  `in-progress` = running (the claim comment says which runner), closed = done with
  evidence.
- Native GitHub notifications when something closes or asks for you.
- A `needs-decision` shows up? Answer **on the issue** ("option B, go") — the runner
  picks it up on its next sweep. You decide without opening a terminal.

## 5. The checkpoint (e.g. Monday morning)

Review is three filters, not a hunt across terminals:

1. `label:needs-decision` — what needs you, each with options and a recommendation
   already written.
2. Closed issues — each with a result comment and the acceptance actually executed.
3. The PRs the runners prepared via `/rp-pr` — self-reviewed, tested, CI green —
   waiting for your merge. The ask-gates guaranteed none merged without you.

## The inversion

You stop being the bus between the list and the terminals and become the arbiter:
write intent once, decide at the marked points, review results with evidence. The full
trail — who, when, why, validated how — is the issues' timeline.
