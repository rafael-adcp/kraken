# AGENTS.md — running as a kraken tentacle

This file lets an `agents.md`-aware agent CLI (GitHub Copilot CLI, and others that
auto-load `AGENTS.md`) act as a **kraken tentacle** — a named worker draining the task
queue in a kraken coordination repo — **by reusing the reference worker skill rather than
forking it**. It is the second implementation whose existence proves the protocol is
agent-agnostic: it carries no copy of the contract, and — since the worker skill became a
loop around `kraken.py next-action` — **no deltas either**.

> If you are a human or an agent **contributing to the kraken codebase itself** (not
> draining a queue), ignore this file and follow [`CONTRIBUTING.md`](CONTRIBUTING.md).

## Your operating contract — read these, then follow them

1. [`PROTOCOL.md`](PROTOCOL.md) — the normative wire contract (`kraken-protocol/8`): the
   label state machine, the hidden `<!-- kraken {...} -->` markers, the claim algorithm,
   delivery, and the authorization boundaries. Agent-agnostic already.
2. [`skills/unleash/SKILL.md`](skills/unleash/SKILL.md) — how a worker *executes* that
   contract. **Read it in full and follow it.** Every transition runs through the bundled
   [`skills/unleash/kraken.py`](skills/unleash/kraken.py) — the **same** program the
   Claude Code worker calls, driven by the **same** `next-action` loop.
3. [`skills/unleash/DELIVERY.md`](skills/unleash/DELIVERY.md) — the delivery conventions
   SKILL.md points you at: branch naming, the attribution trailers, the draft PR, and
   `Closes`. It names your `Co-Authored-By` identity
   (`Co-Authored-By: GitHub Copilot <noreply@github.com>`) alongside Claude Code's.

You are invoked with the same three required arguments SKILL.md documents — the
coordination repo slug `OWNER/tasks`, a `--worker-name` (an identity that says where the
work ran, e.g. `kraken-copilot-1`), and a `--project` (only take `project:<name>` tasks).

## Why there are no deltas any more

SKILL.md used to assume two Claude Code facilities, and this file used to substitute for
them. It no longer has to, because the skill now states both as harness-neutral options:

- **The watcher.** SKILL.md asks for a *persistent background process* running
  `kraken.py watch`, and names the Claude Code Monitor tool and
  [`scripts/kraken-loop.sh`](scripts/kraken-loop.sh) as the two ways to get one. Use the
  loop; it is the versioned home of exactly this fallback.
- **Per-task context isolation.** SKILL.md calls the subagent an explicit optimization
  and says to run the task inline when the harness has none. A fresh `copilot` process
  per pass gives you the same isolation anyway.

The claim algorithm, the `next-action` envelope, the `kraken.py` transitions, the lease
and its renewal, and the authorization boundaries are shared and unchanged. There is
nothing left for this file to override — if you ever find yourself wanting to substitute
something in SKILL.md, that is a bug in SKILL.md, not a delta belonging here.

## Driving it non-interactively

From a checkout of the work repo, with this `AGENTS.md` auto-loaded:

```bash
copilot -p "Act as kraken worker <worker-name>, draining project:<name> from OWNER/tasks.
Follow AGENTS.md, SKILL.md and PROTOCOL.md. Do ONE drain pass: run kraken.py next-action,
execute the task it hands you end to end, deliver it as a draft PR, then stop." \
  --allow-all-tools --no-ask-user --silent
```

Wrap it in a shell loop for continuous ambush — the operator owns the cadence and the
stop. Rather than hand-rolling that loop, use the shipped
[`scripts/kraken-loop.sh`](scripts/kraken-loop.sh): a self-locating, argument-driven
ambush loop you run straight from a kraken checkout (no copying it out of a session
folder):

```bash
scripts/kraken-loop.sh OWNER/tasks --worker-name <worker-name> --project <name>
```

Each poll runs the free, read-only `list-startable` check first and only invokes
`copilot` when a task is actually startable, so an idle queue never spends a token. Pass
`--once` for a single bounded drain, or `--poll <seconds>` to change the 60s cadence.

## Self-healing: the lease does it, the loop only does it faster

**The protocol recovers on its own.** Under `kraken-protocol/8` a claim is a **lease**
with a TTL in minutes (PROTOCOL.md §5): a worker renews it while it works, and a worker
that dies simply stops renewing — the next tentacle to read the queue finds the lease
expired and takes the task over. That happens with **nothing installed**, in every
harness, so the recovery-latency gap between a Claude Code session and a `copilot`
process is gone.

The `hooks/` (SessionEnd, StopFailure) and the loop's release-on-exit are an
**optimization** on top of that floor, not the floor itself: `kraken.py` records the open
claim in `$KRAKEN_STATE_DIR/claim-<worker>.json` (default `~/.kraken/`) and every
terminal transition removes it, so if the `copilot` process exits with that file still
present (crash, kill, rate-limit abort) — or the loop is stopped mid-drain (Ctrl-C,
SIGTERM) — `scripts/kraken-loop.sh` runs `kraken.py release` on the spot and the task
requeues in **seconds instead of at the TTL**. Nothing normative depends on it.

So a **bare** `copilot -p …` invocation outside the loop is not a correctness problem,
only a slightly slower one: its abandoned task comes back within one lease TTL. And a
stale scratch file no longer jams the worker either — `next-action` proves the lease
before resuming, and clears the file itself when the claim is provably gone (`abandon`)
or already resolved.

**What you owe the lease:** renew it. You are not asked to remember the numbers — the
`next-action` envelope's `lease` block carries `renew_every_seconds`, `expires_at` and
`renew_now`, and `then.renew` is the exact command. Stop renewing and you lose the task;
a `heartbeat`, `deliver`, `escalate` or `release` that exits `10` is telling you it
already happened.
