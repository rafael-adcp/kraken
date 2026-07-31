# The Kraken Coordination Protocol — revision history

The normative specification is [`PROTOCOL.md`](PROTOCOL.md). This file is the
**record of how it got there**: one entry per revision, each stating what
changed, why the previous design made it necessary, whether it breaks
compatibility, and what it retired.

Nothing here is normative. A worker is conforming if it satisfies
[`PROTOCOL.md`](PROTOCOL.md) as written — reading this file is never required to
implement one. It exists because the *reasoning* behind a retired mechanism is
what stops it from being reinvented, and because a reader who meets the spec as
a finished object deserves to know which problems each rule was paid for.

Entries are newest first. A backward-incompatible change bumps the integer and
gets an entry; clarifications and strictly additive rules amend
[`PROTOCOL.md`](PROTOCOL.md) in place and do not.

---

**What changed in `kraken-protocol/8`.** The **requeue derivation became
symmetric**. Through protocol/7 the two held states were asymmetric: a bare
operator comment requeued `needs-decision`, but `awaiting-merge` was already
*delivered* and stayed held unless the reply carried an explicit directive — a
standalone `requeue:` line, or a pasted `requeue` marker. The asymmetry was
protecting one real case: a comment that is not a work request ("nice, thanks",
"merging tomorrow", "cc @someone") would bounce a ready branch back to a worker
that then finds a thread asking for nothing.

It was the wrong trade, and the failure mode is worse than the one it prevented.
A review comment asking for follow-up work is *read, matched against a directive
that is not there, and discarded* — silently. Nothing in the GitHub UI says a
comment was considered and dropped; the task simply sits in `awaiting-merge`
looking queued while every worker in ambush is idle and correct. The operator is
the one who eats that silence, and the only way out is to remember a line shape
the queue never asked for. Meanwhile the cost the asymmetry was avoiding is one
wasted drain.

protocol/8 collapses the rule to the one §6 already applied to
`needs-decision`: an operator comment newer than the newest worker comment
requeues, whatever held label the task wears. Everything else in §6 is unchanged
— the live-lease-outranks-the-thread rule, the marker-based worker/operator
discriminator, the read-not-mutation property, the bounded comment window. Both
existing operator paths keep working for free: removing the label by hand still
requeues (the label is what is read), and a standalone `requeue:` line still
requeues (it is a comment — now an ordinary one, with no special form). If a
branch is ready and there is nothing to ask for, the gesture is **merge it**, not
comment on it.

The trade is accepted deliberately rather than avoided, and it is bounded by a
new **SHOULD** in §6: a worker that claims a task requeued this way and finds no
actionable request escalates (§7) instead of guessing at rework. That turns the
worst case from wasted rework into a question on the thread, which makes the
change strictly better than protocol/7 — where the same comment produced nothing
at all.

This is a backward-incompatible change — a protocol/7 reader and a protocol/8
reader return different verdicts for the same thread (delivered task, bare reply:
held vs. queued), so mixed-version workers on one repo disagree about whether a
task is claimable — hence the integer bump. It needs **no migration**, but the
fleet **should be upgraded together**: a protocol/7 worker left running against a
protocol/8 queue is not wrong, only deaf to exactly these tasks. Retired: the
`awaiting-merge` requeue directive in every form — the standalone `requeue:`
line as a *special* shape, the `requeue` marker type (§4), and the
`has_requeue_directive` reader that recognized both.

**What changed in `kraken-protocol/7`.** The `in-progress` label became
**write-only**. Through protocol/6 it was the claim ref's *projection* and it was
**read in decisions**: it made a task `held`, so the claim guard had to refuse on
it — but only conditionally, since the lease behind it might have expired, which
forced the guard to consult the lease mid-refusal. Two sources of truth for one
fact then had to be kept in agreement, and two of the reconciler's four rules
(§6) existed for nothing else: restore a label a crashed claim never wrote, strip
one a crashed release never removed.

protocol/7 observes that only the label's *readers* made any of that necessary.
The **lease alone** decides whether somebody is working on a task (§5); the label
is written for the human scanning the GitHub issue list and is **never** consulted
— not by the claim guard, not by the startable filter, not by the requeue
derivation, not by an operator console. Writers are unchanged: every transition
still sets and clears it exactly as before, so the queue still reads the same in
the UI. What goes away is the branch, the disagreement, and the repairs for it —
the label may lag the lease, and lagging costs a stale badge and nothing else.

This is a backward-incompatible change — a protocol/6 worker treats an
`in-progress` label as holding, so in a mixed fleet it will skip a task a
protocol/7 worker leaves labeled and correctly considers free — hence the integer
bump. It needs **no migration**: nothing about the label's *writes* changed, and a
protocol/7 worker takes a strict superset of the tasks a protocol/6 worker would.
Retired: reconciler rules 3 (**heal**) and 4 (**orphan projection**), and the
claim guard's `in-progress` exception (§5.2).

**What changed in `kraken-protocol/6`.** The claim ref became a **lease**, and
the claim ref name gained a **generation**. Through protocol/5 the ref was a lock
that never expired: it held until its holder deleted it, and a holder that died
left it standing until some reader ran the reconciler's staleness rule — a
threshold measured in **hours**, which is why the lifecycle hooks and the loop's
release-on-exit existed at all. They were covering for the protocol.

protocol/6 observes that the clock is already on the wire — the claim ref's
commit date — and gives it an **expiry**: a lease older than the TTL (order of
minutes) holds nothing, and the **reader** applies that. Taking an expired lease
then has to be as safe as taking a free task, which is what the generation is
for: the holder of a task is whoever owns its **highest** generation, and the
only way to become the holder is to **create the next one** (§5.2). A first
claim, a steal and a renewal are therefore the same conflict-failing `create` on
the same ref name, nothing is ever deleted to make room, and exactly one worker
can win — including when a thief and the live holder's renewal collide. The
heartbeat became that **renewal** rather than a liveness note, and every write of
a transition must first prove the lease is still its author's, so a worker that
comes back after being stolen from stops instead of writing.

This is a backward-incompatible change — a protocol/5 worker neither renews nor
expires, so a protocol/6 worker would steal its live claim after one TTL while it
would never steal one back — hence the integer bump. It needs **no migration**: a
bare `refs/kraken/claims/N` reads as generation 0, the lowest rung. Retired: the
hours-scale staleness threshold (`MAX_HOURS`) and the reclaim-every-stale-ref
rule; escalation on expiry now happens only for a task that keeps expiring (§6).

**What changed in `kraken-protocol/5`.** Reconciliation became an obligation of
the **reader**, not of the coordination repo. Through protocol/4 the reconciler
(§6) was a scheduled job the coordination repo ran on a cron, which made "the
queue repo executes code" a load-bearing assumption: it required CI to be
enabled on a private repo, it bounded recovery by the scheduling interval, and
it forced a copy of the transition program to be vendored into the queue repo
for that job to execute. protocol/5 observes that a stale claim obstructs
exactly one party — the next worker that wants to claim — and that no other
observer of it exists. So the reconcile moves into the queue read that worker
already performs: same four rules, same ref-anchored clock, same ordering,
performed by whoever reads the queue rather than by a scheduler, and at
read time rather than up to an interval later. The **requeue** moved the same
way: through protocol/4 a workflow watched the comment stream and *removed* the
holding label when the operator replied; protocol/5 derives that fact from the
thread instead — an operator comment newer than the newest worker comment — so
no job watches anything and there is no window in which the queue and the thread
disagree. This is a backward-incompatible change — a protocol/4 worker assumes
the repo repairs itself and requeues on its behalf, so pointing one at a
protocol/5 queue leaves stale claims unreclaimed and answered tasks unclaimed —
hence the integer bump. Retired: the requirement that a coordination repo run a
scheduled reconciler or a requeue trigger, and with the latter, the author-type
(`user.type == Bot`) gate the requeue used to need.

**What changed in `kraken-protocol/4`.** Claiming became a true **compare-and-swap
on a git ref**. Through protocol/3 the claim was arbitrated *after the fact*:
because adding a label and posting a comment both succeed for every racer, the
winner could only be decided by re-reading the whole comment thread and applying
the **claim window** — the machinery (its reset events, first-claim-wins,
heartbeat-never-resets) existed solely to stop a dead worker's old claim comment
from winning forever. protocol/4 replaces that with the one GitHub write that
*fails on conflict*: creating the claim ref `refs/kraken/claims/<issue>` (§5).
The server accepts exactly one creator and answers HTTP 422 to everyone else, so
the ref **is** the arbiter and the loser writes nothing. Liveness moves onto the
ref too: its commit's server-stamped date is the reaper's clock (§6), and the
reaper becomes a **reconciler** between the refs (the lock) and the labels (the
projection). This is a backward-incompatible change — a protocol/3 worker
arbitrates comments and never sees a ref — so it bumps the integer. Retired: the
claim window, its reset events, comment-arbitrated ownership, and the heartbeat
comment. Markers (§4) remain, now as **audit trail and operator directives
only** — they never arbitrate a claim.

**What changed in `kraken-protocol/3`.** The retired protocol/1 **visible line
grammar** (`^<keyword>: <value>` scanned per line) is no longer read at all.
protocol/2 required consumers to dual-read both the hidden marker and the
legacy line grammar so pre-existing threads kept arbitrating; protocol/3 drops
that requirement — a conforming consumer reads **only** the hidden marker (§4).
Because no consumer parses visible prose as a machine line, free text in a
comment (a result file, a question, a release reason) **cannot** forge a machine
line — the entire prefix-collision fragility class is gone structurally, not by
escaping.

**What changed in `kraken-protocol/2`.** Machine payloads moved from the
visible line grammar of protocol/1 (`^<keyword>: <value>` scanned per line) to
a structured **hidden marker** — an HTML comment carrying JSON (§4). The visible
prose in a marker-carrying comment is a human-facing courtesy and MUST NOT be
machine-parsed.
