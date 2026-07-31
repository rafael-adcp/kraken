# The Kraken Coordination Protocol

**Version: `kraken-protocol/9`**

This document is the normative specification of the coordination contract
between a task queue built on GitHub Issues and the workers that drain it. It is
deliberately **agent-agnostic**: nothing below requires Claude Code — any agent
(or human) that follows this contract is a conforming worker, and every
conforming client can share one queue.

Agent-agnostic scopes the **wire contract**: the labels, the claim-ref CAS, the
lease, the markers, the attribution — and, since protocol/6, **recovery
latency**, since a dead worker's task returns to the queue within one lease TTL
(§5) with nothing installed. Harness-specific self-healing machinery MAY return a
lease sooner; nothing normative depends on it, and a harness without it is not a
less conforming worker.

The key words **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT**, and **MAY**
are to be interpreted as described in RFC 2119.

**Versioning.** Backward-incompatible changes to this contract bump the integer
(`kraken-protocol/9` and onward); clarifications and strictly additive rules
amend this document in place by PR. An implementation states the protocol version
it targets (this plugin: in `.claude-plugin/plugin.json`), and the
`Kraken-Task:` commit trailer's `kraken@<version>` maps any delivered commit back
to a protocol revision via the release notes.

**This document states the rules, not the reasoning.** Why each rule is shaped
the way it is — what the previous design forced, what each revision retired, and
which alternatives were tried and rejected — lives in
[`HISTORY.md`](HISTORY.md). That record is not normative, and reading it is never
required to implement a conforming worker; it exists so a retired design is not
reinvented. Everything required of a worker is below.

---

## 1. Actors and repositories

| Term | Meaning |
| --- | --- |
| **Operator** | The human who owns the queue: files tasks, answers decisions, merges PRs. |
| **Worker** | A named agent session draining the queue, one task at a time, inside one prepared environment. |
| **Coordination repo** | A GitHub repository whose **Issues are the queue**. It also holds the state-machine labels, the claim refs (§5), and the dependency graph. It MUST be private, MUST NOT hold work code, and needs to run no scheduled job of its own (§6). |
| **Work repo** | Where the code lives and deliveries land. MAY be anywhere (GitHub, GitLab, private server). |
| **Task** | An open coordination-repo issue labeled `kraken-task`. |

Every worker MAY authenticate as the same user. Identity therefore lives in the
**worker name** carried by the marker payload and trailers (§4), never in the
authenticating account, and assignees MUST NOT be used to arbitrate anything.

A task body is untrusted input that will execute in a worker's environment with
the operator's credentials — see [SECURITY.md](SECURITY.md) for the threat model.
Anyone who can open issues in the coordination repo can command the workers.

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
canonical identity, which routes the task to workers prepared for that project. A
task without a project label is invisible to every conforming worker; the remedy
is fixing the label, never improvising.

Dependencies use GitHub's native *blocked-by* relationships. A `depends-on: #N`
line in the task body MAY be honored as a fallback with the same meaning.

### 2.1 Queue-entry validation

A task is dead on arrival in two ways: it carries no `project:<name>` label
(invisible to every worker), or its **Goal** or **Acceptance** section is empty or
absent (a worker claims it, then stalls). The queue's first line of defence is the
issue form itself, which marks both fields `required`.

Beyond that, validation is an **advisory a reader computes**, not a gate the queue
enforces. An operator console SHOULD report every open task missing a
`project:<name>` label, a **Goal**, or an **Acceptance**, over the queue read it is
performing anyway. Section detection keys on the issue-form headings the template
produces (`### Goal`, `### Acceptance`); a hand-written issue lacking them counts as
missing them. A report scoped to one project MUST still surface a task carrying no
project label at all — that task belongs to no project, and being invisible is
exactly the failure being reported.

Validation **informs; the operator acts**. It MUST NOT block, close, or relabel a
task into a held state, and a worker MUST NOT refuse a task on these grounds — a
claimed task whose Goal is unusable is an escalation (§7), not a validation
failure. An implementation MAY instead (or additionally) comment the same three
findings on the issue; such a comment MUST carry a `validation` marker (§4) and
SHOULD refresh the task's state record as §3.1 requires of any machine-authored
comment, so it is not counted as an operator reply.

## 3. The state machine

A task's state has two authorities and one projection. What the task is *between*
workers — queued, blocked on a decision, delivered — is the **state record**
(§3.1), a ref in the coordination repo. Whether a worker is *executing* it right
now is the **lease** (§5). The four labels are the human-facing projection of
both: every transition writes them, so the GitHub UI is the dashboard and the
issue timeline is the log.

| Label | State | Suggested color |
| --- | --- | --- |
| `kraken-task` | queued (when no other state label is present) | `1D76DB` blue |
| `in-progress` | claimed by a worker and being executed | `FBCA04` yellow |
| `needs-decision` | blocked on the operator's decision | `D93F0B` red |
| `awaiting-merge` | delivered, waiting for review + merge | `0E8A16` green |

`project:<name>` (suggested `5319E7` purple) is routing identity, not state.
`priority:high` (suggested `B60205` red) is a scheduling preference, not state: a
startable task carrying it is offered ahead of normal ones (§5), but honoring it
is optional — a worker that ignores it drains in pure `createdAt` FIFO, so the
label is not a coordination invariant and needs no `PROTOCOL_VERSION` bump. Colors
and label descriptions are SHOULD; the label *names* are MUST.

**Every state label is write-only.** A worker **MUST** write them — add
`in-progress` on a won claim, swap it for the held label on escalation and
delivery, clear it on release — because the human reading the issue list has
nothing else to read. A conforming reader **MUST NOT** consult `in-progress` in
any decision: not the claim guard (§5.2), not the startable filter, not the
requeue derivation (§6), not an operator console. It projects the lease, and a
badge that lags its lease is stale, never a coordination error. Nothing repairs
it. `needs-decision` and `awaiting-merge` project the record, and a reader
consults them in exactly one case, named in §3.1: when the task has no record at
all.

A task is **held** when its state record names `needs-decision` or
`awaiting-merge` and §6's requeue derivation has not lifted it, **or when a live
claim ref exists for it** (§5); it is **startable** when it is open, labeled
`kraken-task` + `project:<name>`, not held, and every blocked-by issue is closed. A
task MUST carry at most one state label at a time.

Legal transitions and who performs them:

| Transition | Actor | Mechanism |
| --- | --- | --- |
| queued → `in-progress` | worker | claim — create the claim ref, then project the label (§5) |
| `in-progress` → `needs-decision` | worker | escalation (§7) |
| `in-progress` → `needs-decision` | reconciler | staleness (§6) |
| `in-progress` → `awaiting-merge` | worker | delivery (§8) |
| `in-progress` → queued | worker | release (§9) |
| `needs-decision` → queued | operator | comment on the thread — every reader derives the requeue from the record (§6) |
| `awaiting-merge` → queued | operator | the same gesture and the same derivation (§6) |
| `needs-decision` / `awaiting-merge` → queued | claiming worker | the held label is swapped for `in-progress` when the derived-requeued task is claimed (§5) |
| `awaiting-merge` → closed | merge | the PR's `Closes` reference (§8), or a manual close |

Every transition that *ends* a worker's turn — escalation, delivery, release, and
the reconciler's staleness rule — writes the record as well as the labels (§3.1).
The claim does not: the lease is what says a task is being executed. The
operator's requeue writes nothing at all — it is a comment, and the requeue is
derived from it on the read.

Workers MUST NOT close task issues: "done" for a worker means *delivered for
review*. Closing an issue is cancellation (operator) or landing (merge).

### 3.1 The state record

The state record of task `N` is a ref **`refs/kraken/state/N`** in the
coordination repo, pointing at a commit whose message is a `state` marker (§4).
Like a claim commit it SHOULD be an orphan over the empty tree, so it can be
written without reading repository state.

| Field | Requirement | Meaning |
| --- | --- | --- |
| `state` | MUST | `queued`, `needs-decision` or `awaiting-merge` |
| `worker` | MUST | the worker that wrote the record — audit, never an arbiter |
| `comments` | MUST | the issue's **total comment count** at the moment of the transition; the anchor §6's requeue derivation compares against |
| `expiries` | MUST | how many times a lease on this task has expired and been taken over, cumulative (§6) |
| `pr` | MAY | the delivery URL (§8) |

The record never names `in-progress`. Execution is the lease's business (§5), and
a record says what the task is *between* workers, which is why a claim writes no
record and a task under way keeps the one it arrived with.

**Absence is meaningful.** A task with no record reads as `queued` with
`expiries: 0`, so an untouched queue costs nothing and no queue needs a migration
step. **When — and only when — a task has no record**, a held label is honored:
one carrying `needs-decision` or `awaiting-merge` is held, with no requeue
derivable for it, and the operator's escape hatch is to remove the label by hand.
That fallback is what makes a queue written by an older revision safe to read,
and §6's rule 3 turns it into a record on the first reconcile.

Where a record exists it **decides**, and a reader **MUST NOT** consult the held
labels at all. In particular **removing a held label by hand does not requeue a
task that has a record** — the gesture is to comment, which requeues both held
states (§6).

**Writing.** The record is not a compare-and-swap: it has no generations, and a
writer creates the ref when it is absent and force-updates it when it is present.
Nothing needs to arbitrate, because the two writers are already serialized by
something else. A **transition** writes it while holding the task's lease, which
§5.3 makes it prove before its first write; the **reconciler** (§6) writes it only
as a repair, from rules that are idempotent, so two readers reconciling
concurrently write the same record and converge.

A transition **MUST** read `comments` back from the issue **after** its own comment
has landed, rather than adding one to a count it read before: a comment that
arrives in between is otherwise consumed silently, which is the one way this
mechanism can lose an operator's words. In a `queued` record `comments` decides
nothing — the derivation ranges over held states only (§6) — but it is still
written, so no later transition has to invent it.

A **machine-authored comment posted without a lease** — the §2.1 validation
comment is the only one this contract defines — **SHOULD** refresh the
`comments` field of the record if the task has one, so its own comment does not
read as an operator reply (§6). A producer that does not is noisy, never corrupt:
the task requeues, and the worker that claims it finds nothing actionable and
escalates (§6).

**Ordering.** A transition writes its comment, then the record, then the labels,
then deletes the claim ref (§5.3). The record **MUST** land before the claim ref
is deleted: the ref is what holds the task, so a task freed with no record is
observably queued while it is in fact delivered. A record write that fails
therefore **MUST** fail the transition — the task stays held by the lease it
still owns, which is the correct state to be stuck in, and the transition is
retried.

## 4. The machine marker and attribution

Kraken's machine payloads ride one **hidden marker**: an HTML comment carrying a
compact single-line JSON object, of the form

```
<!-- kraken {"type":"claim","worker":"env-1"} -->
```

The same grammar is used in two kinds of place, and which one a marker rides
decides what a consumer may believe about it: the **commit message of a ref** —
a claim ref (`claim` / `heartbeat`, §5/§6) or a state record (`state`, §3.1) —
where the marker **is** the wire state, and the body of a **state-changing
comment** (the rest), where it is the record of something the refs already
decided. **Grammar** (normative):

- The marker opens with the literal delimiter `<!-- kraken ` (the token `kraken`,
  then a space), followed by the JSON object, then the literal closing delimiter
  ` -->`. Consumers SHOULD match it as `<!--\s*kraken\s+(\{…\})\s*-->`.
- The JSON object MUST be a single line (no embedded newline) and MUST carry a
  string **`type`** field naming the transition. Producers MUST encode it with a
  real JSON serializer, never string interpolation. Producers SHOULD emit
  ASCII-only JSON.
- A state-changing comment MUST carry exactly one marker. A malformed marker
  (undecodable JSON, no string `type`) MUST be ignored, never guessed.

| `type` | Fields | Rides | Posted by | Meaning |
| --- | --- | --- | --- | --- |
| `claim` | `worker` | claim ref commit (and the projection comment) | claim | the worker holding the lease (§5) |
| `heartbeat` | `worker`, `msg`? | claim ref commit | heartbeat | lease renewal; its commit date is the lease timestamp (§5, §6) |
| `state` | `state`, `worker`, `comments`, `expiries`, `pr`? | state record commit | every terminal transition, the steal, the reconciler | the task's operator-facing state (§3.1) |
| `needs-decision` | `worker` | comment | escalation | question posted, decision pending (§7) |
| `delivered` | `worker`, `pr`? | comment | delivery | result posted, review pending (optional `pr` URL) (§8) |
| `released` | `worker`, `reason`? | comment | release | claim handed back (optional `reason`) (§9) |
| `lease-expired` | `worker`, `previous_worker`, `age_seconds`? | comment | claim (steal) | an expired lease was taken over; the count that §6 acts on lives in the record (§3.1), not in the thread |
| `stale-claim` | `reason`? | comment | reconciler | a stale/orphaned claim was reclaimed (§6) |
| `note` | `worker` | comment | note | free-form worker comment (assumptions/progress); carries **no** machine state — inert to reap/requeue/validate |
| `validation` | — | comment | validator | task fails the queue-entry gate; the comment lists what to fix (§2.1) |

**Comment markers are audit trail, never the arbiter** — with no exception left.
Every marker in the table that rides a comment records something a ref already
decided: ownership by the claim-ref CAS (§5), state by the record (§3.1). No
consumer reconstructs either from the comment thread, and no rule reads a comment
by type, by author or by content — §6 reads only **how many** there are.

**Reading (markers only).** A conforming consumer reads machine state from the
hidden marker and **nothing else**: it MUST NOT parse the visible prose of a
comment as a machine line. A line of free text that happens to begin with
`released:`, `delivered:`, `claimed-by:`, `heartbeat:`, or any other former keyword
is inert — it can never occupy a machine-line position.

Every worker-posted coordination-repo comment MUST open with the **attribution
disclaimer**, so a human reading the timeline can tell a tentacle's comment from
another human's even though both may authenticate as the same user:

```
> 🐙 **Kraken worker `<worker-name>`** — automated comment from a kraken tentacle, not a human.
```

The disclaimer is deliberately agent-agnostic — it names no implementation ("a
kraken tentacle", never "a Claude Code tentacle") — so every conforming worker
emits the identical line. It sits *above* the prose and marker with a blank line
between, or GitHub folds the body into the quote. The block above is
**illustrative**: the reference implementation defines the format once and derives
every other occurrence from it.

Nothing machine-facing depends on telling a worker's comment from an operator's:
§6 counts comments instead of classifying them, so a worker's own comment moves
the same counter an operator's does, and the record written by the same transition
is what accounts for it (§3.1). The disclaimer is attribution, and only that.

## 5. The claim algorithm — a lease on a git ref

Claiming is the only contended transition, and it is a **compare-and-swap on a git
ref**. The claim of issue `N` is a ref `refs/kraken/claims/N/<generation>` in the
coordination repo, where the generation is a positive integer: creating a ref is
the one GitHub write that **fails on conflict** — the server accepts exactly one
creator and answers HTTP 422 to everyone else — so the ref's existence *is* the
lock.

**The holder of a task is whoever owns its highest generation**, and the only way
to become the holder is to **create the next one**. A bare `refs/kraken/claims/N`
reads as generation **0**, so an existing queue needs no migration.

Each claim ref MUST point at a commit whose message is the `claim` (or
`heartbeat`, §6) marker naming the worker (§4). The commit SHOULD be an orphan (no
parents) over the empty tree, so it can be created without reading repository
state.

### 5.1 The lease

That ref is a **lease**, not a lock held forever. Its terms:

- **The clock is the ref's commit date**, stamped by the server.
- **Both ends of the comparison are the server's clock.** A lease's age is the
  server's *now* minus that server-stamped date; a reader **MUST NOT** subtract it
  from its own wall clock. A reader that has no reading of the server's clock at
  all MAY fall back to its own; it MUST NOT prefer it.
- **The TTL is a duration in minutes**, configurable, and it is the worst case for
  how long a dead worker's task stays unavailable. The reference implementation
  defaults to **1800 seconds (30 minutes)**. It MUST be long enough that a live
  worker in a single long silent step is not stolen from.
- **Renewal is derived from the TTL**, not configured beside it: a worker holding a
  lease **SHOULD** renew it every **TTL/3**, so two consecutive renewals may be
  lost before anyone else may take the task. Renewal is the heartbeat (§6).
- **The reader applies the expiry.** A lease whose age is **≥ TTL** holds nothing:
  a reader **MUST** treat it as expired — it does not make a task `held`. No job,
  no pass and no operator is involved in an expiry; it is a property of the read.
- **An unreadable clock is an expired lease.** If the ref's commit (or its date)
  cannot be read, a reader **MUST** treat the lease as expired — failing **open**,
  toward the steal.

### 5.2 Claiming, and stealing

Stealing an expired lease is the same algorithm from a different starting rung: a
first claim creates generation 1, a steal creates the generation above the expired
holder's, and a renewal (§6) creates the generation above its own. The sequence is
fixed:

1. **List** startable candidates (§3), `priority:high` first and then oldest first
   by `createdAt`, a stable order that keeps `createdAt` FIFO within each priority
   tier. Label filtering SHOULD be done client-side for determinism.
2. **Dependency check**: skip any candidate whose blocked-by issues are not all
   closed.
3. **Guard**: re-derive the task's state over a fresh read — its record, and the
   requeue derivation §6 applies to it (§3.1). If it is still held, the worker
   MUST skip it without writing anything. Whether the task is held is the only
   thing this step asks; no label answers it unless the task has no record.

   A **live lease that is not ours holds the task whatever the labels say**,
   including in the crash window between a won CAS and its label projection. A
   worker MUST NOT attempt the CAS there — the generation it would create is the
   *next* one, so the attempt would succeed and take a live lease. The verdict is a
   **loss**, not an unclear task.
4. **CAS**: create the claim commit, then create the ref
   `refs/kraken/claims/N/<G+1>` pointing at it, where `G` is the highest generation
   observed on the issue (`0` when it carries no claim ref at all). **HTTP 422**
   means another worker got that generation first: the worker MUST back off having
   **written nothing** and move on. Any other failure leaves the claim state
   unknown — re-check before retrying.
5. **Collect** the generations below the won one. They are superseded, so this step
   MAY fail without affecting correctness.
6. **Project** (only after the CAS is won, in this order): record local claim state
   (so a lifecycle hook can auto-release); if this was a **steal**, post the
   `lease-expired` comment and write the state record with `expiries` incremented
   by one, every other field left as it stood (§3.1); then add the `in-progress`
   label and post the `claim` comment. A first claim writes no record — it changes
   nothing about what the task is between workers. A failure at this step leaves
   the task **held by the lease**, which is the only thing that was holding it
   anyway; nothing is expected to repair the missing badge, and an `expiries`
   increment that did not land costs one further steal before §6's guard fires.

### 5.3 Prove the lease before writing

A worker **MUST** confirm that the lease is still its own **before the first write**
of any transition that records an outcome — escalation (§7), delivery (§8), release
(§9). That proof is also what lets the state record (§3.1) be written without a
CAS of its own: the lease is what serializes its writers. "Its own" means it still owns the **highest** generation: a worker's own ref
surviving proves nothing, because a thief takes the task by creating the generation
*above* it. If the highest generation belongs to another worker, or there is no
claim ref at all, the worker **MUST** write nothing and report the loss. An
*ambiguous* read (transport failure) is not a loss: the worker MUST re-check rather
than write on a guess. A lease that is expired but still **ours** does not refuse.

Renewal needs no separate check — it **is** one: renewing means creating the next
generation, so a stolen-from worker is told by the CAS itself (§6).

Implementations MUST make the verdict available as a **status/exit code**, not as
text a caller has to match against.

A worker MUST work one task at a time: it MUST NOT claim a second task while it
holds a lease (or while a claim of its own is in an unknown state after a network
failure — re-check first).

**Releasing the lease.** Every terminal transition (escalation §7, delivery §8,
release §9) and the reconciler (§6) MUST delete the claim ref — **every generation
of it**, so no superseded rung is left to read as a lease later — and MUST do so
**last**, after its comment, its state record and its label writes have landed, so
the task is never observably free while a transition is half-applied. Deleting an already-absent ref is a success.
Releasing early is an **optimization**, not an obligation: a worker that simply
stops still frees its task within one TTL.

## 6. Lease renewal and the reconciler

- **Renewal.** A worker holding a lease **SHOULD** renew it every **TTL/3** while
  executing (§5.1): **create the next generation** of its claim ref, pointing at a
  fresh commit (a `heartbeat` marker, optionally carrying a `msg` progress field),
  then collect the generation it left behind. The server-stamped date of that commit
  restarts the TTL. It posts no comment.

  A renewal and a steal of the same lease **cannot both succeed** — they race to
  create the same ref name. A worker whose renewal is refused (HTTP 422) has been
  stolen from: it **MUST** treat that as losing the lease and stop, rather than
  continue work on a task another worker now owns.

  A worker that stops renewing need do nothing else: its task returns to the queue
  within one TTL. Releasing on the way out (§9) is an **optimization** that returns
  it in seconds instead.
- **The reader reconciles.** A worker **MUST** reconcile the claim refs with the
  queue before it claims, over the queue read it is performing anyway. A conforming
  coordination repo runs **no** scheduled job, and a conforming worker **MUST NOT**
  assume one exists. An operator MAY additionally run the same pass by hand; doing
  so is an ergonomic, never a precondition.

  The reconcile is about the refs, and only the refs. An **expired lease** is not
  its business — expiry is applied on the read (§5.1) and the next claim steals it
  — and neither is a stray `in-progress` label, which holds nothing (§3). Anything
  it decides about a lease is anchored to the **claim ref's commit date**, **not**
  the issue's `updatedAt`, so nothing on the issue timeline extends one. The
  reconcile applies:
  1. **Orphan ref** — a claim ref or a state record on an issue that is no longer an
     open task, or a claim ref on a task whose record already names a held state (a
     terminal transition whose ref delete was lost): delete that ref, touch nothing
     else.
  2. **Repeatedly expired** — a task whose record reports `expiries` ≥ **N** (N
     configurable; the reference implementation uses **3**): move it to
     `needs-decision` — the `stale-claim` comment, then the record, then the label —
     and delete the claim ref. A **live** lease is never reclaimed by this rule,
     however long the task's history of steals.
  3. **Unrecorded held state** — an open task carrying `needs-decision` or
     `awaiting-merge` and holding **no** state record: write the record its label
     already implies, with `expiries: 0` and `comments` read as the thread stands
     now. It touches no label, posts no comment and deletes no ref. This is what
     migrates a queue written before the record existed, and what lets a conforming
     reader tolerate a writer that still only sets labels.

  Rules 1 and 2 are keyed on a ref, so a task with neither is never their business,
  whatever labels it wears; rule 3 is keyed on the absence of one, and a task whose
  record and label already agree is nobody's business. The rules follow §5's
  ordering — writes first, **ref delete last** — so a reconcile interrupted part-way
  leaves the task held rather than observably free, and the next reader's rule 1
  finishes it. Every rule is idempotent, so two workers reconciling concurrently
  converge. The reconciler's comments are worker comments and carry the §4
  disclaimer like any other.
- **A new comment requeues by derivation.** The operator's gesture is just
  "comment" — never "comment **and** remove the label". A reader **MUST** treat a
  held task as queued when the issue's **total comment count** — every comment on
  the thread, whoever wrote it — is **greater than its record's `comments`** (§3.1).
  The transition recorded what the thread carried at the moment it delivered or
  escalated, so anything added since is by definition something the task has not
  been read against. The record and the holding label are stale at that point, and
  the claiming worker swaps the label off as part of the claim (§5). This is a
  **read**, not a mutation: nothing is written to lift a hold.

  The derivation opens no comment. It does not classify an author, match a
  directive, or parse a body — it compares two integers, one of which rides the
  same read that lists the queue, so every reader reaches the same verdict at the
  same cost.

  A **live lease outranks the count**: a comment on a claimed task is context for
  its worker, never a requeue. An **expired** lease outranks nothing — it holds no
  task (§5.1), so it cannot suppress a requeue either.

  The rule is the **same for both held states**: one new comment requeues
  `needs-decision` and `awaiting-merge` alike. There is no directive to remember and
  no line shape to get right. **Any** comment counts, a worker's and a bot's
  included — which is exactly why a transition records the count *including the
  comment it just posted*, and why a machine-authored comment written without a
  lease refreshes the record it did not write (§3.1).

  A worker that claims a task requeued this way and finds **no actionable request**
  in what was added **SHOULD** escalate (§7) rather than guess at rework. Delivered
  work stays delivered — the branch and PR are untouched.

  The derivation ranges over the two held states only. `in-progress` is not one of
  them and never needs lifting: it holds nothing (§3). A task with no record has no
  count to compare against, so a held label with no record stays held until rule 3
  above gives it one.

## 7. Escalation

When a task is blocked on a decision only the operator can make (an unverifiable
assumption whose failure would be expensive, an ambiguous goal), the worker MUST
escalate rather than guess: prove the lease is still its own (§5.3), then post the
question — options and a recommendation — with a `needs-decision` marker, write the
state record naming `needs-decision` and the comment count that includes that
question (§3.1), swap `in-progress` for `needs-decision`, and delete the claim ref
(§5). The comment MUST land before the record, the record before the label swap,
and the ref delete last, so a half-executed escalation leaves the task held rather
than free with no question on record.

The operator answers on the thread. That comment is all that is needed: it puts the
thread one ahead of the record, the next reader derives the requeue from it (§6),
and whoever claims the task inherits the full thread as context.

## 8. Delivery

Work left in a working tree evaporates with the environment; delivery is what makes
it real. Unless the task's notes say otherwise:

- Deliver on a **work branch** following the work repo's own naming convention (no
  evident convention → a neutral descriptive name including the task number),
  pushed, with a **draft PR/MR** describing what, why, and how it was validated.
- Every delivered commit MUST carry the attribution trailers:

  ```
  Co-Authored-By: <agent identity> <noreply@...>
  Kraken-Task: <coordination-repo>#<issue> (worker: <worker-name>, kraken@<version>)
  ```

- The PR body SHOULD carry `Closes <coordination-repo>#<issue>` when the work repo
  is on GitHub — merging then closes the task at the moment the work truly lands,
  which is also what unblocks dependents. Elsewhere, reference the task as text.
- On the task issue: **prove the lease is still this worker's** (§5.3), then post the
  result comment — what was done, how the **acceptance was executed** and its real
  outcome, links — with a `delivered` marker (carrying the `pr` URL field when there
  is one), write the state record naming `awaiting-merge`, carrying that same URL in
  its `pr` field and the comment count that includes the result comment (§3.1), swap
  `in-progress` for `awaiting-merge`, then delete the claim ref (§5). Comment first,
  record second, labels third, ref last. If the lease was stolen while this worker
  was silent, the result MUST NOT be posted — the branch and PR still exist, and
  re-claiming the task delivers them.
- A work repo that cannot take a branch push: put the full diff or patch in the
  result comment and flag it — work MUST NOT be silently lost.

**The record's `pr` field is the delivery URL.** A consumer that needs the delivery
PR (a review console, a dashboard, an orphan check) MUST read it from the state
record (§3.1) and MUST NOT derive it from any comment — not from the visible prose,
and not from the `delivered` marker, which is audit trail like every other comment
marker (§4). There is no fallback: a delivered task whose record carries no `pr` is
a delivery that has no PR — the case §8 allows above — and MUST be presented as
that, never as a link to go looking for.

## 9. Release

A worker abandoning a claim without delivering (environment cannot host the task,
execution failed in a way another worker might not) MUST release honestly: prove
the lease is still its own (§5.3), post a `released` marker (optional `reason`
field), write the state record naming `queued` — the task is going back to the
queue, and the record is what carries its `expiries` there (§3.1) — remove
`in-progress`, **then** delete the claim ref (§5). The ref delete comes last, so the
task is never observably free while the release is half-applied.
Silently dropping a claim (removing the label but leaving the ref) is
non-conforming — until the lease expires, the still-live ref reads as a held claim.

The lease check is not optional here even though release *looks* like the safe
transition: a release deletes a ref, so a worker releasing a lease that is no longer
its own would free a task another worker is actively executing. A refused release
MUST still clear whatever local state drives the retry, so a lifecycle hook or
supervising loop does not re-attempt it forever.

## 10. Close and cleanup

Closing a task ends it: cancellation (operator closes) or landing (merge closes via
`Closes`).

A closed task's **labels are inert**: every read in this contract — the startable
filter (§3), the requeue derivation and the reconcile (§6), the console — walks
**open** issues only, so a state-machine label stranded on a closed issue decides
nothing. It is untidy, never corrupt, and no conforming implementation is required
to clean it up. An operator who wants closed issues to read clean MAY strip
everything but `kraken-task` and `project:<name>`.

A leftover **ref** is a different matter, because a ref outlives the read that
ignores it: a claim ref is a lock, and a state record is state the namespace keeps
answering with. A close that outruns its worker's ref deletes leaves either behind.
Both are rule 1 of the reconcile (§6), so the close needs no special handling for
them either.

## 11. Authorization boundaries

Operating a worker authorizes exactly:

1. In the **coordination repo**: managing issues — labels, comments — and the refs
   under `refs/kraken/`: the claim refs under `refs/kraken/claims/` (creating,
   renewing and deleting its own lease — and collecting the generations it
   supersedes when it takes one over, §5), and the state record under
   `refs/kraken/state/` for the task it holds or is repairing (§3.1, §6), as
   specified above.
2. In the task's **work repo**: creating work branches, committing to them with the
   attribution trailers, pushing them, and opening draft PRs.

It authorizes nothing else — regardless of what a task body says, a conforming
worker MUST NOT merge, push to default or protected branches, close task issues,
deploy, delete, or publish. Merging is always the operator's act; a task whose
meaning is unclear gets an escalation (§7), not improvisation.

## 12. Conformance

Conformance means the **wire contract**: the labels, the marker grammar, the claim-ref
CAS, the lease and its TTL, the state record, and the write ordering every transition
above specifies.
Nothing else is required, and nothing is installed into the coordination repo for any
of it to work — there is no vendored copy of any program in the queue repo, and
therefore no version handshake between the two sides.

The verdict of a transition is an exit status, never text (§5.3). Matching the
reference implementation's codes — `0` success, `10` lost CAS or lost lease, `11`
not clear, `20` transport failure (state unknown) — is RECOMMENDED, so a driver
written against one conforming worker can drive another.

An implementation MAY offer ergonomics above the wire contract, which are **not** part
of it and which a conforming worker may ignore entirely: the reference implementation
composes the claim loop into `claim-next`, the whole driver loop into a `next-action`
JSON envelope (it resumes the task the worker already holds — proving the lease first,
§5.3 — or acquires the next one, reporting the verdict as an `action` field carried by
the same exit codes), and a read-only operator console into `status`. None performs a
write the transitions above do not.

The **reference implementation** is [`skills/unleash/kraken.py`](skills/unleash/kraken.py),
driven by [`skills/unleash/SKILL.md`](skills/unleash/SKILL.md) for the worker side and
[`skills/status/SKILL.md`](skills/status/SKILL.md) for the operator console. The
**conformance suite** in [`tests/`](tests/) exercises this contract's invariants against
a stateful GitHub stub; a third-party implementation MAY validate itself by pointing that
suite at its own transition executables.
