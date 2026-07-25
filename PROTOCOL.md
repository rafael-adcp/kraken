# The Kraken Coordination Protocol

**Version: `kraken-protocol/6`**

This document is the normative specification of the coordination contract
between a task queue built on GitHub Issues and the workers that drain it. It
is deliberately **agent-agnostic**: nothing below requires Claude Code — any
agent (or human) that follows this contract is a conforming worker, and every
conforming client can share one queue. Agent-agnostic scopes the **wire
contract** — the labels, the claim-ref CAS, the lease, the markers, the
attribution — and as of protocol/6 that contract also covers **recovery
latency**: a dead worker's task returns to the queue within one lease TTL (§5)
for every conforming worker, with nothing installed. Harness-specific
self-healing machinery — the bundled Claude Code lifecycle hooks, or
`scripts/kraken-loop.sh`'s release-on-exit for Copilot CLI — is an
**optimization** on top of that floor, never a requirement: it returns a lease
in seconds rather than at the TTL, and nothing normative depends on it.

The key words **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT**, and **MAY**
are to be interpreted as described in RFC 2119.

**Versioning.** Backward-incompatible changes to this contract bump the
integer (`kraken-protocol/6` and onward); clarifications and strictly additive
rules amend this document in place by PR. An implementation states the protocol
version it targets (this plugin: in `.claude-plugin/plugin.json`), and the
`Kraken-Task:` commit trailer's `kraken@<version>` maps any delivered commit
back to a protocol revision via the release notes.

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

## 1. Actors and repositories

| Term | Meaning |
| --- | --- |
| **Operator** | The human who owns the queue: files tasks, answers decisions, merges PRs. |
| **Worker** | A named agent session draining the queue, one task at a time, inside one prepared environment. |
| **Coordination repo** | A GitHub repository whose **Issues are the queue**. It also holds the state-machine labels, the claim refs (§5), and the dependency graph. It MUST be private, MUST NOT hold work code, and — as of protocol/5 — needs to run no scheduled job of its own (§6). |
| **Work repo** | Where the code lives and deliveries land. MAY be anywhere (GitHub, GitLab, private server). |
| **Task** | An open coordination-repo issue labeled `kraken-task`. |

Every worker MAY authenticate as the same user. Identity therefore lives in
the **worker name** carried by the marker payload and trailers (§4), never in
the authenticating account, and assignees MUST NOT be used to arbitrate
anything.

A task body is untrusted input that will execute in a worker's environment
with the operator's credentials — see [SECURITY.md](SECURITY.md) for the
threat model. Anyone who can open issues in the coordination repo can command
the workers.

## 2. Task shape

A task is created from the queue's issue template
([`skills/unleash/task-template.yml`](skills/unleash/task-template.yml)) and
carries three fields:

| Field | Requirement | Contract |
| --- | --- | --- |
| **Goal** | MUST | The desired end state, written as an outcome. What the worker plans toward and restates as assumptions. |
| **Acceptance** | MUST | Executable, observable proof the goal was met. A worker MUST run it for real before delivering; a task whose acceptance was not executed MUST NOT move to `awaiting-merge`. |
| **Notes** | MAY | Constraints, frozen contracts, gotchas. |

Every task MUST carry exactly one **`project:<name>`** label — the project's
canonical identity, which routes the task to workers prepared for that
project. A task without a project label is invisible to every conforming
worker; the remedy is fixing the label, never improvising.

Dependencies use GitHub's native *blocked-by* relationships. A `depends-on:
#N` line in the task body MAY be honored as a fallback with the same meaning.

### 2.1 Queue-entry validation

The two ways a task is dead on arrival — no `project:<name>` label (invisible to
every worker) or an empty/absent **Goal** or **Acceptance** section (a worker
claims it, then stalls) — are the queue's most common operator mistakes, and
they are otherwise silent. The queue's **first line of defence is the issue form
itself**: the bundled template marks Goal and Acceptance `required`, so the form
refuses a blank field before the issue exists.

Beyond that, validation is an **advisory a reader computes**, not a gate the
queue enforces: an operator console SHOULD report every open task missing a
`project:<name>` label, a **Goal**, or an **Acceptance**, over the queue read it
is performing anyway (the reference implementation does this in `status`). Section
detection keys on the issue-form headings the template produces (`### Goal`,
`### Acceptance`); a hand-written issue lacking them counts as missing them. A
report scoped to one project MUST still surface a task carrying no project label
at all — that task belongs to no project, and being invisible is exactly the
failure being reported.

Validation **informs; the operator acts**. It MUST NOT block, close, or relabel
a task into a held state, and a worker MUST NOT refuse a task on these grounds —
a claimed task whose Goal is unusable is an escalation (§7), not a validation
failure. An implementation MAY instead (or additionally) comment the same three
findings on the issue; the reference implementation keeps that as an
operator-invoked `validate` subcommand, debounced so a re-run adds no duplicate,
and its comment carries a `validation` marker so the §6 requeue derivation reads
it as machine-authored.

## 3. Labels: the state machine

Four labels are the entire state machine. Every transition is a label change,
so the GitHub UI is the dashboard and the issue timeline is the log.

| Label | State | Suggested color |
| --- | --- | --- |
| `kraken-task` | queued (when no other state label is present) | `1D76DB` blue |
| `in-progress` | claimed by a worker and being executed | `FBCA04` yellow |
| `needs-decision` | blocked on the operator's decision | `D93F0B` red |
| `awaiting-merge` | delivered as a draft PR, waiting for review + merge | `0E8A16` green |

`project:<name>` (suggested `5319E7` purple) is routing identity, not state.
`priority:high` (suggested `B60205` red) is a scheduling preference, not state:
a startable task carrying it is offered ahead of normal ones (§5), but honoring
it is optional — a worker that ignores it simply drains in pure `createdAt` FIFO,
so the label is not a coordination invariant and needs no `PROTOCOL_VERSION` bump.
Colors and label descriptions are SHOULD (they make every kraken queue read
the same — see `skills/init/SKILL.md`); the label *names* are MUST.

The `in-progress` label is the **projection** of the claim ref (§5), not the
lock itself: a worker sets it right after it wins the CAS, and the reconciler
(§6) restores the two to agreement if a crash leaves them out of step. The other
two held labels are set by uncontended transitions (§7, §8).

A task is **held** when it carries `in-progress`, `needs-decision`, or
`awaiting-merge`, **or when a claim ref exists for it** (§5); it is **startable**
when it is open, labeled `kraken-task` + `project:<name>`, not held, and every
blocked-by issue is closed. A task MUST carry at most one held label at a time —
stacking them (e.g. `in-progress` + `awaiting-merge`) is state corruption, and
the claim guard (§5) exists to prevent it.

Legal transitions and who performs them:

| Transition | Actor | Mechanism |
| --- | --- | --- |
| queued → `in-progress` | worker | claim — create the claim ref, then project the label (§5) |
| `in-progress` → `needs-decision` | worker | escalation (§7) |
| `in-progress` → `needs-decision` | reconciler | staleness (§6) |
| `in-progress` → `awaiting-merge` | worker | delivery (§8) |
| `in-progress` → queued | worker | release (§9) |
| `needs-decision` → queued | operator | reply on the thread — every reader derives the requeue from it (§6); or remove the label by hand |
| `needs-decision` → queued | claiming worker | the label is swapped for `in-progress` when the derived-requeued task is claimed (§5) |
| `awaiting-merge` → queued | operator | review feedback on the thread, then remove the label (a bare comment does NOT requeue — see §6) |
| `awaiting-merge` → closed | merge | the PR's `Closes` reference (§8), or a manual close |

Workers MUST NOT close task issues: "done" for a worker means *delivered for
review*, and the task closes when the work truly lands. Closing an issue is
cancellation (operator) or landing (merge).

## 4. The machine marker and attribution

Kraken's machine payloads ride one **hidden marker**: an HTML comment carrying a
compact single-line JSON object, of the form

```
<!-- kraken {"type":"claim","worker":"env-1"} -->
```

The same marker grammar is used in two places: the **commit message of a claim
ref** (`claim` / `heartbeat` — §5, §6) and the body of a **state-changing
comment** (the rest). The marker is invisible in GitHub's rendered timeline, so
the surrounding prose is a pure human courtesy. **Grammar** (normative):

- The marker opens with the literal delimiter `<!-- kraken ` (the token
  `kraken`, then a space), followed by the JSON object, then the literal
  closing delimiter ` -->`. Consumers SHOULD match it as
  `<!--\s*kraken\s+(\{…\})\s*-->`.
- The JSON object MUST be a single line (no embedded newline) and MUST carry a
  string **`type`** field naming the transition. Producers MUST encode it with
  a real JSON serializer (never string interpolation) — this is what retires
  the CRLF/quoting hazard class. Producers SHOULD emit ASCII-only JSON so the
  marker never carries a byte a `C`-locale filter could miss.
- A state-changing comment MUST carry exactly one marker. A malformed marker
  (undecodable JSON, no string `type`) MUST be ignored, never guessed.

| `type` | Fields | Rides | Posted by | Meaning |
| --- | --- | --- | --- | --- |
| `claim` | `worker` | claim ref commit (and the projection comment) | claim | the worker holding the lease (§5) |
| `heartbeat` | `worker`, `msg`? | claim ref commit | heartbeat | lease renewal; its commit date is the lease timestamp (§5, §6) |
| `needs-decision` | `worker` | comment | escalation | question posted, decision pending (§7) |
| `delivered` | `worker`, `pr`? | comment | delivery | result posted, review pending (optional `pr` URL) (§8) |
| `released` | `worker`, `reason`? | comment | release | claim handed back (optional `reason`) (§9) |
| `lease-expired` | `worker`, `previous_worker`, `age_seconds`? | comment | claim (steal) | an expired lease was taken over; **counted** by §6's repeat-expiry guard |
| `stale-claim` | `reason`? | comment | reconciler | a stale/orphaned claim was reclaimed (§6) |
| `note` | `worker` | comment | note | free-form worker comment (assumptions/progress); carries **no** machine state — inert to reap/requeue/validate (SKILL.md §2a) |
| `requeue` | — | comment | operator | bounce a delivered (`awaiting-merge`) task back for rework (§6) |
| `validation` | — | comment | validator | task fails the queue-entry gate; the comment lists what to fix (§2.1) |

**Markers are audit trail and directives, never the arbiter.** Under protocol/4
the claim is decided by the ref CAS (§5), not by reading markers: the `claim`
and `heartbeat` markers on the ref carry worker identity and progress for
`status` to render, and the comment markers (`needs-decision`, `delivered`,
`released`, `lease-expired`, `stale-claim`) are the human-facing record of a
transition plus the `pr`/`reason` fields tooling reads. `lease-expired` is the
one marker a rule also **counts** rather than merely displays (§6's
repeat-expiry guard) — it still arbitrates no claim. The `note` marker rides a free-form worker
comment and records nothing — it changes no label and no claim ref; it exists
only so a note is recognizable as worker-authored (below). No consumer
reconstructs ownership from the comment thread.

**Reading (markers only).** A conforming consumer reads machine state from the
hidden marker and **nothing else**: it MUST NOT parse the visible prose of a
comment as a machine line. A line of free text that happens to begin with
`released:`, `delivered:`, `claimed-by:`, `heartbeat:`, or any other former
keyword is inert — it can never occupy a machine-line position.

Every worker-posted coordination-repo comment MUST open with the
**attribution disclaimer** — every worker may authenticate as the operator,
so the disclaimer is what lets a *human reading the timeline* distinguish
tentacle comments from human ones:

```
> 🐙 **Kraken worker `<worker-name>`** — automated comment from a kraken tentacle, not a human.
```

The disclaimer is deliberately **agent-agnostic**: it names no implementation
("a kraken tentacle", never "a Claude Code tentacle"), so every conforming worker
— whatever agent drives it — emits the *identical* line and the timeline reads
uniformly. This is what lets a second implementation drain the same queue without
diverging the human-vs-tentacle discriminator.

**The machine discriminator is the hidden marker, not the disclaimer.** Every
worker-posted comment also carries a marker (a transition marker for a state
change, the `note` marker for a free-form note), and marker presence is the
normative worker-comment discriminator: a consumer that must tell a worker
comment from an operator reply (the §6 requeue derivation) treats a
comment as worker-authored when it **carries any valid kraken marker**, falling
back to a first line opening with the disclaimer prefix (`> 🐙 **Kraken worker \``)
for legacy or marker-less threads. The disclaimer stays a MUST as the human-facing
attribution, but it is **no longer load-bearing** for requeue — machine semantics
ride the marker, presentation text does not. **Accepted edge:** an operator who
pastes a raw kraken marker into a reply is read as a worker and will not requeue
(the same class of edge as an operator quoting the disclaimer on the first line);
removing the held label by hand is the escape hatch.

The disclaimer sits *above* the prose and marker with a blank line between, or
GitHub folds the body into the quote. The block above is **illustrative**: the
Kraken reference implementation defines the format once as the `DISCLAIMER`
constant in `skills/unleash/kraken.py` and every other occurrence derives from it
— `kraken.py contract disclaimer` prints the authoritative line, and consumers
that must recognize a worker comment (the §6 requeue derivation) are
verified against it by executing both rather than by copying the literal.

## 5. The claim algorithm — a lease on a git ref

Claiming is the only contended transition, and it is a **compare-and-swap on a
git ref**. The claim of issue `N` is a ref `refs/kraken/claims/N/<generation>` in
the coordination repo, where the generation is a positive integer: creating a ref
is the one GitHub write that **fails on conflict** — the server accepts exactly
one creator and answers HTTP 422 to everyone else — so the ref's existence *is*
the lock, and no consumer ever reconstructs ownership from the timeline.

**The holder of a task is whoever owns its highest generation**, and the only way
to become the holder is to **create the next one**. A bare
`refs/kraken/claims/N` — the shape protocol/5 wrote — reads as generation **0**,
so an existing queue needs no migration: it is simply the lowest rung, and the
first protocol/6 claim over it creates generation 1.

Each claim ref MUST point at a commit whose message is the `claim` (or
`heartbeat`, §6) marker naming the worker (§4). The commit SHOULD be an orphan
(no parents) over the empty tree, so it can be created without reading repository
state.

### 5.1 The lease

That ref is a **lease**, not a lock held forever. Its terms:

- **The clock is the ref's commit date.** The server stamps it, so the timestamp
  is not something a worker can get wrong, and reading it costs nothing extra —
  it comes with the queue read (§6).
- **The TTL is a duration in minutes**, configurable, and it is the worst case
  for how long a dead worker's task stays unavailable. The reference
  implementation defaults to **1800 seconds (30 minutes)** and exposes it as
  `kraken.py contract lease-ttl`. It MUST be long enough that a live worker in a
  single long silent step is not stolen from.
- **Renewal is derived from the TTL**, not configured beside it: a worker
  holding a lease **SHOULD** renew it every **TTL/3** (`contract lease-renew`),
  so two consecutive renewals may be lost before anyone else may take the task.
  Renewal is the heartbeat (§6).
- **The reader applies the expiry.** A lease whose age is **≥ TTL** holds
  nothing: a reader **MUST** treat it as expired — it does not make a task
  `held`, and neither does the `in-progress` label projected from it. No job,
  no pass and no operator is involved in an expiry; it is a property of the
  read.
- **An unreadable clock is an expired lease.** If the ref's commit (or its date)
  cannot be read, nothing proves the holder alive, and a reader **MUST** treat
  the lease as expired — failing **open**, toward the steal. A lease must never
  be able to hold a task forever behind a clock nobody can read.

### 5.2 Claiming, and stealing

The sequence is fixed:

1. **List** startable candidates (§3), `priority:high` first and then oldest
   first by `createdAt`, a stable order that keeps `createdAt` FIFO within each
   priority tier. Label filtering SHOULD be done client-side for determinism.
2. **Dependency check**: skip any candidate whose blocked-by issues are not
   all closed.
3. **Guard**: re-fetch the issue's current labels. If it is now held, the
   worker MUST skip it without writing anything. `in-progress` is the exception
   that proves the lease rule: it is a lease's *projection*, so it holds only
   while the lease behind it does — a worker MUST consult the lease before
   refusing on that label alone.

   Conversely, a **live lease that is not ours holds the task whatever the
   labels say**, including in the crash window between a won CAS and its label
   projection. A worker MUST NOT attempt the CAS there: the generation it would
   create is the *next* one, so the attempt would succeed and take a live lease.
   The read answers it instead, and the verdict is a **loss**, not an unclear
   task — somebody owns it.
4. **CAS**: create the claim commit, then create the ref
   `refs/kraken/claims/N/<G+1>` pointing at it, where `G` is the highest
   generation observed on the issue (`0` when it carries no claim ref at all).
   **HTTP 422** ("Reference already exists") means another worker got that
   generation first: the worker MUST back off having **written nothing** and
   move on. Any other failure leaves the claim state unknown — re-check before
   retrying.
5. **Collect** the generations below the won one. They are superseded: the
   holder is the highest, so a delete that fails leaves a stray ref, never a
   second lease, and this step MAY fail without affecting correctness.
6. **Project** (only after the CAS is won, in this order): record local claim
   state (so a lifecycle hook can auto-release), post the `lease-expired`
   comment if this was a steal, add the `in-progress` label, then post the
   `claim` comment (disclaimer + prose; the machine payload already rides the
   ref). A failure at this step leaves the task **held by the lease** — the
   reconciler (§6) heals a missing label on its next pass.

### 5.2.1 Why a steal is not a special case

Stealing an expired lease is **the same algorithm with a different starting
rung**: a first claim creates generation 1, a steal creates the generation above
the expired holder's, and a renewal (§6) creates the generation above its own.
All three are the identical conflict-failing `create` on the identical ref name.

That is where the uniqueness guarantee comes from, and it holds without
qualification:

- **N thieves racing one expired lease** all try to create generation `G+1`. The
  server accepts one. Every other gets 422 and writes nothing.
- **A thief racing the holder's renewal** is the same race: the renewal is also a
  `create` of `G+1`, so either the holder keeps the lease and the thief is told
  it lost, or the thief takes it and the *holder* is told — immediately, by its
  own renewal failing, not later by some reconciliation.
- **Nothing is deleted to make room.** There is no instant at which the task is
  observably unheld, no ordering in which one worker's write erases another's,
  and no need for a post-CAS confirmation: winning the create *is* holding the
  lease, because a later generation can only be created by someone who has
  already observed this one.

A reader's observation of `G` may be stale — it costs a lost CAS, never a lost
lease. There is no claim window and no reset events; the constructs that existed
only to compensate for writes that could not fail on conflict stay retired.

### 5.3 Prove the lease before writing

A worker **MUST** confirm that the lease is still its own **before the first
write** of any transition that records an outcome — escalation (§7), delivery
(§8), release (§9). "Its own" means it still owns the **highest** generation: a
worker's own ref surviving proves nothing, because a thief takes the task by
creating the generation *above* it. If the highest generation belongs to another
worker, or there is no claim ref at all, the worker **MUST** write nothing and
report the loss. An *ambiguous* read (transport failure) is not a loss: the
worker MUST re-check rather than write on a guess.

Renewal needs no separate check — it **is** one: renewing means creating the next
generation, so a stolen-from worker is told by the CAS itself (§6).

This is what makes a short TTL safe. A worker that stalled long enough to be
stolen from — a suspended machine, a rate-limit wait, an hour-long build — is
indistinguishable from a live one until it tries to write, and a result written
onto a task another worker is now executing is worse than no result: it lies to
the review queue, and a release would delete the live holder's lease. Nothing is
lost by refusing — the branch and PR still exist, and re-claiming the task
delivers them honestly. A lease that is expired but still **ours** does not
refuse: it expired, nobody took it, and completing the transition frees it
honestly.

Implementations MUST make the verdict available as a **status/exit code**, not
as text a caller has to match against.

A worker MUST work one task at a time: it MUST NOT claim a second task while it
holds a lease (or while a claim of its own is in an unknown state after a
network failure — re-check first).

**Releasing the lease.** Every terminal transition (escalation §7, delivery §8,
release §9) and the reconciler (§6) MUST delete the claim ref — **every
generation of it**, so no superseded rung is left to read as a lease later — and
MUST do so **after** its comment and label writes land, so the task is never
observably free while a transition is half-applied. Deleting an already-absent
ref is a success (the delete is idempotent). Releasing early is an
**optimization**, not an obligation: a worker that simply stops still frees its
task within one TTL.

## 6. Lease renewal and the reconciler

- **Renewal.** A worker holding a lease **SHOULD** renew it every **TTL/3**
  while executing (§5.1): **create the next generation** of its claim ref,
  pointing at a fresh commit (a `heartbeat` marker, optionally carrying a `msg`
  progress field), then collect the generation it left behind. The
  server-stamped date of that commit restarts the TTL. It posts no comment, so a
  long task does not flood the timeline.

  Renewal is a CAS for the same reason a steal is, and on the same ref name — so
  a renewal and a steal of the same lease **cannot both succeed**. A worker whose
  renewal is refused (HTTP 422) has been stolen from: it **MUST** treat that as
  losing the lease and stop, rather than continue work on a task another worker
  now owns. This is the earliest and cheapest signal a live-but-slow worker
  gets.

  A worker that stops renewing does not need to do anything else: its task
  returns to the queue within one TTL. Releasing on the way out (§9), by a
  lifecycle hook or by a supervising loop, is an **optimization** that returns it
  in seconds instead — nothing normative depends on it, and a harness without
  such machinery is not a less conforming worker, only a slower-to-recover one.
- **The reader reconciles.** A worker **MUST** reconcile the claim refs (the
  leases) with the `in-progress` labels (the projection) before it claims, over
  the queue read it is performing anyway. Reconciliation is *not* delegated to
  the coordination repo: a stale claim obstructs exactly one party — the next
  worker that wants to claim — and no other observer of it exists, so the party
  that cares is the party that repairs. A conforming coordination repo therefore
  runs **no** scheduled job, and a conforming worker **MUST NOT** assume one
  exists. An operator MAY additionally run the same pass by hand at any time
  (the reference implementation exposes it as `kraken.py reap`); doing so is an
  ergonomic, never a precondition.

  **An expired lease is not one of the reconciler's jobs.** Expiry is applied on
  the read (§5.1) and the next claim steals it, so a reconciler that "repaired"
  an expired lease would only escalate a task the next drain was about to pick
  up. What remains is anchored to the **claim ref's commit date** — **not** the
  issue's `updatedAt`, and nothing on the issue timeline resets it, so a human
  commenting on a dead worker's issue shortens time-to-triage rather than
  extending the lease. The reconcile applies, per claim ref and per stray label:
  1. **Orphan lock** — a ref on an issue that is no longer an open task, or on
     one already labeled `needs-decision`/`awaiting-merge` (a terminal
     transition whose ref delete was lost): delete the ref, touch nothing else.
  2. **Repeatedly expired** — a task whose lease has already expired **N** times
     (N configurable; the reference implementation uses **3**, counted from the
     `lease-expired` markers on the thread): move it to `needs-decision` with a
     `stale-claim` comment, then delete the ref. A short TTL means an abandoned
     task returns to the queue on its own — but a task that kills whatever worker
     touches it would then circulate forever, burning a drain each round and
     telling nobody. Past the threshold it stops being stolen and becomes the
     operator's call. A **live** lease is never reclaimed by this rule, however
     long the task's history of steals: whoever holds it is working.
  3. **Heal** — a **live** lease on an issue missing its `in-progress` label (a
     claim whose label projection did not land): add the label. Restoring the
     label for an *expired* lease would re-hold a task the reader just decided is
     free, so the rule keys on liveness.
  4. **Orphan projection** — an open `in-progress` issue with **no** ref (a
     crashed release, or a claim made before protocol/4): remove the label so
     the task requeues, leaving a `stale-claim` comment saying why.

  The rules are ordered by the same rule §5 gives every transition — writes
  first, **ref delete last** — so a reconcile interrupted part-way leaves the
  task held rather than observably free, and the next reader's rule 1 finishes
  it. Every rule is idempotent, so two workers reconciling concurrently converge
  rather than fight. The reconciler's comments are posted by a worker and
  therefore carry the §4 attribution disclaimer like any other worker comment.
- **An operator reply requeues by derivation.** The operator's gesture is just
  "reply" — never "reply **and** remove the label". A reader **MUST** treat a
  label-held task as queued when the thread carries an operator comment newer
  than its newest worker comment; the holding label is stale projection at that
  point and the claiming worker swaps it off as part of the claim (§5). This is
  a **read**, not a mutation: no job watches the comment stream, and there is no
  window in which the queue and the thread disagree. A reader MAY bound how far
  back it reads the thread — only the newest comment of each class decides the
  verdict, so a window that contains no worker comment means the newest one is
  older than the window, and every operator comment in it is newer.

  The human-vs-tentacle discriminator is §4's hidden marker: a comment carrying
  any valid kraken marker — or, as a legacy fallback, opening with the
  `> 🐙 **Kraken worker …` blockquote — is a worker's; every other comment is the
  operator's. Because *every* kraken-authored comment wears a marker, this also
  covers the coordination repo's own automation, which is what keeps the
  reconciler's `stale-claim` comment from instantly undoing the escalation it
  just posted — protocol/4 needed an author-type gate for that, protocol/5 does
  not.

  A **live lease outranks the thread**: a comment on a claimed task is context
  for its worker, never a requeue. An **expired** lease outranks nothing — it
  holds no task (§5.1), so it cannot suppress a requeue either.

  The two held states are asymmetric: a bare operator comment requeues
  **`needs-decision`** (a human comment is almost always the answer; a "let me
  think" self-corrects via re-escalation), but **`awaiting-merge`** is already
  *delivered* and stays held unless a reply carries an explicit **requeue
  directive** — a standalone `requeue:` line (a line whose only content is
  `requeue`/`requeue:`). A `requeue:` buried in a prose sentence **MUST NOT**
  bounce a ready branch back to a worker. (Because any comment bearing a hidden
  marker reads as worker-authored, a pasted `<!-- kraken {"type":"requeue"} -->`
  marker is subsumed by the §4 accepted edge — the standalone line, or removing
  the label by hand, is the operator's path.)

  `in-progress` is **not** requeueable this way: an `in-progress` label with no
  ref behind it is rule 4 above, a repair, not a requeue.

## 7. Escalation

When a task is blocked on a decision only the operator can make (an
unverifiable assumption whose failure would be expensive, an ambiguous goal),
the worker MUST escalate rather than guess: prove the lease is still its own
(§5.3), then post the question — options and a recommendation — with a
`needs-decision` marker, swap `in-progress` for `needs-decision`, and delete the
claim ref (§5). The comment MUST land before the label swap and the ref delete
last, so a half-executed escalation leaves the task held rather than free with
no question on record.

The operator answers on the thread. That reply is all that is needed: the next
reader derives the requeue from it (§6) — or the operator removes the label by
hand — and whoever claims the task inherits the full thread as context.

## 8. Delivery

Work left in a working tree evaporates with the environment; delivery is what
makes it real. Unless the task's notes say otherwise:

- Deliver on a **work branch** following the work repo's own naming
  convention (CI pipelines key on those patterns; no evident convention → a
  neutral descriptive name including the task number), pushed, with a
  **draft PR/MR** describing what, why, and how it was validated.
- Every delivered commit MUST carry the attribution trailers:

  ```
  Co-Authored-By: <agent identity> <noreply@...>
  Kraken-Task: <coordination-repo>#<issue> (worker: <worker-name>, kraken@<version>)
  ```

- The PR body SHOULD carry `Closes <coordination-repo>#<issue>` when the work
  repo is on GitHub — merging then closes the task at the moment the work
  truly lands, which is also what unblocks dependents. Elsewhere, reference
  the task as text; the operator closes it after merging.
- On the task issue: **prove the lease is still this worker's** (§5.3), then
  post the result comment — what was done, how the **acceptance was executed**
  and its real outcome, links — with a `delivered` marker (carrying the `pr` URL
  field when there is one), swap `in-progress` for `awaiting-merge`, then delete
  the claim ref (§5). Comment first, labels second, ref last (§5's ordering rule,
  same rationale). A delivery is the write the lease check exists for: if the
  lease was stolen while this worker was silent, the result MUST NOT be posted —
  the branch and PR still exist, and re-claiming the task delivers them.
- A work repo that cannot take a branch push: put the full diff or patch in
  the result comment and flag it — work MUST NOT be silently lost.

## 9. Release

A worker abandoning a claim without delivering (environment cannot host the
task, execution failed in a way another worker might not) MUST release
honestly: prove the lease is still its own (§5.3), post a `released` marker
(optional `reason` field), remove `in-progress`, **then** delete the claim ref
(§5). Deleting the ref is what actually frees the task for the next worker; the
comment and label are narrative and projection. The ref delete comes last, so
the task is never observably free while the release is half-applied. Silently
dropping a claim (removing the label but leaving the ref) is non-conforming —
until the lease expires, the still-live ref reads as a held claim.

The lease check is not optional here even though release *looks* like the safe
transition: a release deletes a ref, so a worker releasing a lease that is no
longer its own would free a task another worker is actively executing. A refused
release MUST still clear whatever local state drives the retry, so a lifecycle
hook or supervising loop does not re-attempt it forever.

## 10. Close and cleanup

Closing a task ends it: cancellation (operator closes) or landing (merge closes
via `Closes`).

A closed task's **labels are inert**: every read in this contract — the startable
filter (§3), the requeue derivation and the reconcile (§6), the console — walks
**open** issues only, so a state-machine label stranded on a closed issue decides
nothing. It is untidy, never corrupt, and no conforming implementation is
required to clean it up. An operator who wants closed issues to read clean MAY
strip everything but `kraken-task` and `project:<name>` (the reference
implementation keeps a `cleanup` subcommand for that).

A leftover **claim ref** is a different matter, because a ref is a lock: a close
that outruns its worker's ref delete leaves one behind. That is rule 1 of the
reconcile (§6) — a ref whose issue is no longer an open task is an orphan lock,
deleted by the next reader — so the close needs no special handling for it
either.

## 11. Authorization boundaries

Operating a worker authorizes exactly:

1. In the **coordination repo**: managing issues — labels, comments — and the
   claim refs under `refs/kraken/claims/` (creating, renewing, and deleting its
   own lease — and collecting the generations it supersedes when it takes one
   over, §5) as specified above.
2. In the task's **work repo**: creating work branches, committing to them
   with the attribution trailers, pushing them, and opening draft PRs.

It authorizes nothing else — regardless of what a task body says, a
conforming worker MUST NOT merge, push to default or protected branches,
close task issues, deploy, delete, or publish. Merging is always the
operator's act; a task whose meaning is unclear gets an escalation (§7), not
improvisation.

## 12. Conformance

The **reference implementation** of the worker side is
[`skills/unleash/kraken.py`](skills/unleash/kraken.py) — one stdlib-only program
with a subcommand per transition (`list-startable`, `claim`, `heartbeat`, `note`,
`escalate`, `deliver`, `release`, `watch`), driven by a Claude Code skill
([`skills/unleash/SKILL.md`](skills/unleash/SKILL.md)) that supplies the
judgment between transitions. The transport is direct HTTP over the stdlib
(`urllib`) against the GitHub REST + GraphQL API — the base URL comes from
`GITHUB_API_URL` (default `https://api.github.com`) and the token from
`GH_TOKEN`/`GITHUB_TOKEN`, falling back to one `gh auth token` spawn at startup —
so it runs against any authenticated host without shelling out to `gh` per call.
It also ships a `claim-next OWNER/tasks <project> <worker>`
convenience that composes `list-startable` + `claim` into the whole
deterministic claim loop (reconcile, list, guard, CAS, skip-on-loss, try the
next candidate) behind one invocation — a worker-side ergonomic detail, not part
of the wire contract; it exits `0` holding a won claim (printing the task's
number, title, and body), a distinct `3` when nothing is claimable, and `20` on
transport failure with the same state-unknown semantics. Before it claims, it
also refuses — writing nothing — when this worker already holds an open claim
(`11`, the §5 one-task-at-a-time rule, tracked in a local claim-state file) or
when the repo carries no `project:<name>` label, so the worker would be deaf
rather than idle (`13`). Both are reference-implementation ergonomics, not wire
contract.

Nothing is installed into the coordination repo for any of this to work. The
reference `init` commits one file — the issue-form template — and creates the
labels; there is no vendored copy of the program and therefore no version
handshake between the two sides, because there is no second copy to drift.

A read-only `status OWNER/tasks [--project <name>] [--json]` subcommand
(operator-side, driven by [`skills/status/SKILL.md`](skills/status/SKILL.md))
computes the console — the review queue (`awaiting-merge` + parsed PR link), the
decision queue (`needs-decision`), in-flight tasks with the worker and lease age
read from the claim ref's commit — the same anchor every reader applies the TTL
to (§5.1), flagging the leases that no longer hold —
the merged-PR-but-open-issue orphan heuristic (flag-only, never acting), the
queue-entry hygiene report (§2.1), and the `project:` launch recon — over the same batched queue walk `list-startable`
uses, with the awaiting-merge PR-link history read through the paginated comment
path so it is never truncated past 100 comments. It performs no writes; `--json`
emits a stable schema for downstream tooling. Like `claim-next`, it is a
reference-implementation ergonomic, not part of the wire contract.

The **conformance suite** in [`tests/`](tests/) exercises the contract's
invariants against a stateful GitHub stub — the claim guard, the claim-ref CAS
race (exactly one winner, the loser writing nothing), the **steal race** (N
workers on one expired lease, exactly one winner, the verdict read from exit
codes), lease expiry on the read (renewed holds, past-TTL does not, unreadable
clock fails open), **write-after-expiry** (a resurrected worker's deliver,
escalate, release and renewal all refuse and write nothing), the repeat-expiry
guard, thread-history independence (the comment thread neither blocks nor grants
a claim), the renewal's ref advance, honest release/deliver/escalate deleting the
ref, the reconciler's four rules, failure staging, the marker-only invariant
(free text that starts a line with a former keyword does not forge a machine
line), and the read-only `status` console (ref-anchored age and orphan flagging,
never acting) — plus `kraken.py` unit tests ([`tests/unit/`](tests/unit/)) that
cover the ref CAS helpers, the lease clock, the reconciler classification, marker
decoding edge cases, and comment pagination past 100 in isolation. A third-party
implementation MAY validate itself by pointing the suite's stub at its own
transition executables; matching `kraken.py`'s exit-code contract (`0` success /
`10` lost CAS or lost lease / `11` no longer clear / `20` transport failure) is
RECOMMENDED but the wire contract — labels, the marker grammar, the lease and its
TTL, ordering — is what conformance means.
