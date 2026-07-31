# The Kraken Coordination Protocol — revision history and rationale

The normative specification is [`PROTOCOL.md`](PROTOCOL.md). This file is
everything that is **not** a rule but is worth keeping, in two parts:

1. the **revision history** — one entry per revision, each stating what changed,
   why the previous design made it necessary, whether it breaks compatibility,
   and what it retired;
2. the **standing rationale** ([below](#design-rationale)) — why the rules that
   are *currently* in force are shaped the way they are.

Nothing here is normative. A worker is conforming if it satisfies
[`PROTOCOL.md`](PROTOCOL.md) as written — reading this file is never required to
implement one. It exists because the *reasoning* behind a design is what stops a
retired one from being reinvented, and because a reader who meets the spec as a
finished object deserves to know which problems each rule was paid for.

Revision entries are newest first. A backward-incompatible change bumps the
integer and gets an entry; clarifications and strictly additive rules amend
[`PROTOCOL.md`](PROTOCOL.md) in place and do not.

---

## Revision history

**What changed in `kraken-protocol/9`.** A task's machine state moved out of the
comment thread and into a **ref**: `refs/kraken/state/<issue>` (§3.1), carrying the
operator-facing state, the thread's comment count at the moment of the transition,
the cumulative count of expired leases, and the delivery URL.

Through protocol/8 the two held labels *were* the state, and everything the label
could not say was **derived from the thread**. Whether the operator had replied
meant classifying each comment of a bounded window as worker- or operator-authored.
How many times the lease had expired meant counting `lease-expired` markers — a
count the spec itself admitted undercounts once the window rolls past them. Which
PR was delivered meant the newest `delivered` marker's `pr`, with a free-text
fallback for threads older than the field. Three derivations over one window, each
with its own failure mode, and a comment-hydration pass on every queue read to feed
them.

A record collapses all three into fields. The requeue verdict becomes
`totalCount > record.comments` — one integer against another, and the total rides
the same GraphQL walk that lists the queue, so the derivation costs no call, opens
no comment body, and reaches the same verdict for every reader. `expiries` becomes
exact instead of window-bounded. `pr` becomes a field with no prose behind it.

It also makes §4 true. Since protocol/4 the spec has said comment markers are audit
trail and never an arbiter, while the repeat-expiry guard counted them and the
review console read a delivery URL out of one. Both readers now go to the record,
and the claim is the ref CAS, the state is the record, and a comment decides
nothing — which is the sentence protocol/4 wrote and protocol/9 finally earns.

The requests balance out negative. `refs/kraken/state/` is paginated by the same
`matching-refs/kraken/` read that already fetched the claim refs, and against that
one enlarged read stand the whole comment-hydration pass and a paginated comment
read per `awaiting-merge` task in the console.

This is a backward-incompatible change — a protocol/8 reader derives the requeue by
classifying a comment window and never looks at a record; a protocol/9 reader stops
consulting the held labels the moment a record exists — so mixed-version workers on
one repo disagree about whether a task is claimable. Hence the integer bump, and
the fleet **should** be upgraded together. It needs **no migration step**: rule 3 of
the reconcile writes the record a held label already implies, so a queue heals on
its first drain after the upgrade, and the same rule is what lets a protocol/9
reader tolerate a protocol/8 writer that still only sets labels. **One accepted
edge:** a `needs-decision` task the operator answered *before* the upgrade migrates
with `comments` read as the thread stands, so that answer is consumed and the task
stays held. The remedy is to comment again. Paying it once is cheaper than
resurrecting the classification pass to look for the answer, which is the code this
revision exists to delete.

Retired: the requeue derivation by comment classification and the worker-vs-operator
discriminator it needed (§4); the bounded comment window and the hydration pass that
filled it; the expiry count derived from `lease-expired` markers; the free-text PR
fallback and its `legacy` marking (§8), whose removal protocol/8 had already
deferred to the next incompatible bump; and *removing a held label by hand* as a
requeue gesture — where a record exists the record decides, and the gesture is to
comment, which works on both held states since protocol/8.

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

---

## Design rationale

Why the rules *currently* in force are shaped the way they are. These paragraphs
lived inside [`PROTOCOL.md`](PROTOCOL.md) until the spec was split into rules and
reasoning; they legislate nothing, and each is keyed to the section it explains.

### §3.1 — why the state is a ref, and why the anchor is a count

The record exists because everything it holds used to be *derived* from the
comment thread, and each derivation was paying for the same missing fact: nobody
had written down what the task was. A thread is an append-only log of prose, so
reading state out of one means classifying entries, and classification is where
the failure modes lived — a window that rolled past the markers it was counting, a
pasted marker read as a worker, a PR URL that was only a link somebody typed.

It is a **ref** rather than an issue field because the queue repo installs nothing
(§12): a ref is server-side, cheap to read in bulk, writable with no schema to
migrate, and it is the same primitive the claim already uses, so the reader that
lists claims lists records in the same paginated call.

The anchor is a **count** rather than a timestamp or an author because a count is
the one thing a reader gets for free. `comments.totalCount` rides the queue walk;
comparing it to the number written at the transition asks exactly the question that
matters — *has anything been said since the task was put down?* — with no clock to
reconcile between machines, no author metadata to request, and no body to open. It
deliberately cannot tell an operator's comment from a bot's. protocol/8 already
accepted that trade in the other direction, bounding it with the SHOULD that a
worker finding nothing actionable escalates instead of guessing, and the same bound
covers this.

It is **not** a compare-and-swap because it would gain nothing from being one: the
lease already serializes every writer, and §5.3 makes each of them prove it before
writing. A second lock over a lock is a second thing to reconcile.

### §4 — why comment markers are audit trail and nothing else

The claim is decided by the ref CAS (§5) and the state by the record (§3.1), so
every marker riding a *comment* is describing something already decided elsewhere:
the human-facing record of a transition, plus the `reason` field a console renders.
The `note` marker records nothing at all — it changes no label, no ref and no
state, and exists only so a free-form note is recognizable as worker-authored.

That separation was declared in protocol/4 and only became true in protocol/9. Two
rules kept reading comments as data: the repeat-expiry guard counted `lease-expired`
markers, and the review console read the delivery URL out of a `delivered` marker.
Both now read the record, and no rule anywhere reads a comment's type, author or
body — §6 reads only how many comments there are.

The disclaimer remains required as human-facing attribution, and it is no longer
load-bearing for anything mechanical: protocol/5 demoted it from discriminator to
fallback when every kraken-authored comment gained a marker, and protocol/9 retired
the discriminator itself. Keeping it agent-agnostic is what lets a second
implementation drain the same queue and have the timeline still read uniformly —
every conforming worker, whatever agent drives it, emits the identical line.

### §5.1 — why both ends of the lease clock are the server's

The lease timestamp is a commit date GitHub stamped. Comparing it to local
`time.time()` compares two clocks and calls the difference age: a worker running
five minutes fast steals leases that are still alive, one running five minutes
slow keeps dead ones alive past their TTL. Coordination happens between machines
whose clocks disagree, and the whole disagreement lands in that subtraction.

Reading the server's *now* costs no request — every HTTP response carries a
`Date` header — which is why the rule can be a MUST rather than a nicety. Reading
the lease itself costs nothing extra either: the ref's commit date comes with the
queue read a worker is already performing.

### §5.2 — why a steal is not a special case

Stealing an expired lease is the same algorithm from a different starting rung: a
first claim creates generation 1, a steal creates the generation above the expired
holder's, and a renewal creates the generation above its own. All three are the
identical conflict-failing `create` on the identical ref name.

That is where the uniqueness guarantee comes from, and it holds without
qualification:

- **N thieves racing one expired lease** all try to create generation `G+1`. The
  server accepts one. Every other gets 422 and writes nothing.
- **A thief racing the holder's renewal** is the same race: the renewal is also a
  `create` of `G+1`, so either the holder keeps the lease and the thief is told it
  lost, or the thief takes it and the *holder* is told — immediately, by its own
  renewal failing, not later by some reconciliation.
- **Nothing is deleted to make room.** There is no instant at which the task is
  observably unheld, no ordering in which one worker's write erases another's, and
  no need for a post-CAS confirmation: winning the create *is* holding the lease,
  because a later generation can only be created by someone who has already
  observed this one.

A reader's observation of `G` may be stale — it costs a lost CAS, never a lost
lease. There is no claim window and no reset events; the constructs that existed
only to compensate for writes that could not fail on conflict stay retired.

### §5.3 — why proving the lease is what makes a short TTL safe

A worker that stalled long enough to be stolen from — a suspended machine, a
rate-limit wait, an hour-long build — is indistinguishable from a live one until
it tries to write. A result written onto a task another worker is now executing is
worse than no result: it lies to the review queue, and a release would delete the
live holder's lease.

Nothing is lost by refusing. The branch and PR still exist, and re-claiming the
task delivers them honestly. Without this check the TTL would have to be long
enough to cover the worst silent step any worker might take, which is the same
thing as not having a lease at all.

### §6 — why the reader reconciles, and which rules survive

Reconciliation is not delegated to the coordination repo because a stale claim
obstructs exactly one party — the next worker that wants to claim — and no other
observer of it exists. The party that cares is the party that repairs, and it does
so over a queue read it was performing anyway, so the pass costs nothing and
happens at read time rather than up to a scheduling interval later. That is what
let protocol/5 retire the scheduled job, and with it the requirement that a
private queue repo run CI at all.

Two things are explicitly *not* the reconcile's job. An **expired lease** is not:
expiry is applied on the read (§5.1) and the next claim steals it, so a reconciler
that "repaired" one would only escalate a task the next drain was about to pick
up. A **stray `in-progress` label** is not either, in neither direction: the label
is write-only (§3), so it holds nothing, hides nothing and misroutes nothing, and
the next transition overwrites it. protocol/6 had a rule for each of those two
label cases; protocol/7 retired both.

The repeat-expiry guard exists because a short TTL solves the abandoned task and
creates a new one. An abandoned task returns to the queue on its own — but a task
that *kills whatever worker touches it* would circulate forever, burning a drain
each round and telling nobody. Past the threshold it stops being stolen and
becomes the operator's call.

The third rule, added in protocol/9, is a repair rather than a lease question: a
held label with no state record is a task written by an older revision, and writing
the record its label already implies is what migrates the queue. It is in the
reconcile because that is where repairs go — it runs on the read every worker
already performs, so no operator runs a migration and nothing is installed to run
one for them.

### §8 — why the delivery URL is a field and not a link in the prose

A link found in prose is not a delivery. It can be a PR a human mentioned in
passing, another task's PR, or a `/pull/N` in an unrelated repo — and a reader
that greps for one reports the wrong PR with exactly the same confidence as the
right one. Making the URL a structured field is the §4 marker-only reading rule
applied to the last place where free text was still read as data.

protocol/9 moved the field from the `delivered` comment marker to the state record
for the same reason it moved the rest of the state: a console asking "what is
waiting for review, and where is it" then answers from the refs it already read,
instead of paginating a comment thread per delivered task to find a marker. The
prose fallback protocol/8 kept for pre-field threads went with it — protocol/8 had
already scoped it as removable at the next incompatible bump, and this is that
bump.
