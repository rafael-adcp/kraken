# The Kraken Coordination Protocol

**Version: `kraken-protocol/8`**

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
(`kraken-protocol/8` and onward); clarifications and strictly additive rules
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
findings on the issue; such a comment MUST carry a `validation` marker so the §6
requeue derivation reads it as machine-authored.

## 3. Labels: the state machine

Four labels are the entire state machine. Every transition is a label change, so
the GitHub UI is the dashboard and the issue timeline is the log.

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

The `in-progress` label is **write-only**. It is the human-facing projection of
the claim ref (§5), and a conforming reader **MUST NOT** consult it in any
decision: not the claim guard (§5.2), not the startable filter, not the requeue
derivation (§6), not an operator console. Every transition still writes it (a
worker **MUST** add it on a won claim and clear it on escalation, delivery and
release), but because nothing reads it back, a label that lags the lease is a
stale badge and never a coordination error. Nothing repairs it.

`needs-decision` and `awaiting-merge` have no lock behind them, so the label **is**
the state and readers decide on it directly. They are set by uncontended
transitions (§7, §8).

A task is **held** when it carries `needs-decision` or `awaiting-merge`, **or when
a live claim ref exists for it** (§5); it is **startable** when it is open, labeled
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
| `needs-decision` → queued | operator | reply on the thread — every reader derives the requeue from it (§6); or remove the label by hand |
| `awaiting-merge` → queued | operator | reply on the thread — the same gesture and the same derivation (§6); or remove the label by hand |
| `needs-decision` / `awaiting-merge` → queued | claiming worker | the held label is swapped for `in-progress` when the derived-requeued task is claimed (§5) |
| `awaiting-merge` → closed | merge | the PR's `Closes` reference (§8), or a manual close |

Workers MUST NOT close task issues: "done" for a worker means *delivered for
review*. Closing an issue is cancellation (operator) or landing (merge).

## 4. The machine marker and attribution

Kraken's machine payloads ride one **hidden marker**: an HTML comment carrying a
compact single-line JSON object, of the form

```
<!-- kraken {"type":"claim","worker":"env-1"} -->
```

The same grammar is used in two places: the **commit message of a claim ref**
(`claim` / `heartbeat` — §5, §6) and the body of a **state-changing comment** (the
rest). **Grammar** (normative):

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
| `needs-decision` | `worker` | comment | escalation | question posted, decision pending (§7) |
| `delivered` | `worker`, `pr`? | comment | delivery | result posted, review pending (optional `pr` URL) (§8) |
| `released` | `worker`, `reason`? | comment | release | claim handed back (optional `reason`) (§9) |
| `lease-expired` | `worker`, `previous_worker`, `age_seconds`? | comment | claim (steal) | an expired lease was taken over; **counted** by §6's repeat-expiry guard |
| `stale-claim` | `reason`? | comment | reconciler | a stale/orphaned claim was reclaimed (§6) |
| `note` | `worker` | comment | note | free-form worker comment (assumptions/progress); carries **no** machine state — inert to reap/requeue/validate |
| `validation` | — | comment | validator | task fails the queue-entry gate; the comment lists what to fix (§2.1) |

**Markers are audit trail, never the arbiter.** The claim is decided by the ref CAS
(§5). `lease-expired` is the one marker a rule also **counts** rather than merely
displays (§6's repeat-expiry guard); it still arbitrates no claim. No consumer
reconstructs ownership from the comment thread.

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

**The machine discriminator is the marker, not the disclaimer.** A consumer that
must tell a worker comment from an operator reply (the §6 requeue derivation)
treats a comment as worker-authored when it **carries any valid kraken marker**,
falling back to a first line opening with the disclaimer prefix
(`> 🐙 **Kraken worker \``) for legacy or marker-less threads. **Accepted edge:** an
operator who pastes a raw kraken marker into a reply is read as a worker and will
not requeue; removing the held label by hand is the escape hatch.

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
3. **Guard**: re-fetch the issue's current labels. If it now carries
   `needs-decision` or `awaiting-merge`, the worker MUST skip it without writing
   anything. Those are the only two labels the guard reads.

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
   (so a lifecycle hook can auto-release), post the `lease-expired` comment if this
   was a steal, add the `in-progress` label, then post the `claim` comment. A
   failure at this step leaves the task **held by the lease**, which is the only
   thing that was holding it anyway; nothing is expected to repair the missing
   badge.

### 5.3 Prove the lease before writing

A worker **MUST** confirm that the lease is still its own **before the first write**
of any transition that records an outcome — escalation (§7), delivery (§8), release
(§9). "Its own" means it still owns the **highest** generation: a worker's own ref
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
**after** its comment and label writes land, so the task is never observably free
while a transition is half-applied. Deleting an already-absent ref is a success.
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

  The reconcile is about leases, and only leases. An **expired lease** is not its
  business — expiry is applied on the read (§5.1) and the next claim steals it — and
  neither is a stray `in-progress` label, which holds nothing (§3). What remains is
  anchored to the **claim ref's commit date**, **not** the issue's `updatedAt`, so
  nothing on the issue timeline extends a lease. The reconcile applies, per claim
  ref:
  1. **Orphan lock** — a ref on an issue that is no longer an open task, or on one
     already labeled `needs-decision`/`awaiting-merge` (a terminal transition whose
     ref delete was lost): delete the ref, touch nothing else.
  2. **Repeatedly expired** — a task whose lease has already expired **N** times (N
     configurable; the reference implementation uses **3**, counted from the
     `lease-expired` markers on the thread): move it to `needs-decision` with a
     `stale-claim` comment, then delete the ref. A **live** lease is never reclaimed
     by this rule, however long the task's history of steals.

  Both rules are keyed on a claim ref, so a task with no ref is never this pass's
  business, whatever labels it wears. The rules follow §5's ordering — writes first,
  **ref delete last** — so a reconcile interrupted part-way leaves the task held
  rather than observably free, and the next reader's rule 1 finishes it. Every rule
  is idempotent, so two workers reconciling concurrently converge. The reconciler's
  comments are worker comments and carry the §4 disclaimer like any other.
- **An operator reply requeues by derivation.** The operator's gesture is just
  "reply" — never "reply **and** remove the label". A reader **MUST** treat a
  label-held task as queued when the thread carries an operator comment newer than
  its newest worker comment; the holding label is stale projection at that point and
  the claiming worker swaps it off as part of the claim (§5). This is a **read**, not
  a mutation. A reader MAY bound how far back it reads the thread — only the newest
  comment of each class decides the verdict, so a window that contains no worker
  comment means the newest one is older than the window, and every operator comment
  in it is newer.

  The discriminator is §4's hidden marker: a comment carrying any valid kraken
  marker — or, as a legacy fallback, opening with the `> 🐙 **Kraken worker …`
  blockquote — is a worker's; every other comment is the operator's.

  A **live lease outranks the thread**: a comment on a claimed task is context for
  its worker, never a requeue. An **expired** lease outranks nothing — it holds no
  task (§5.1), so it cannot suppress a requeue either.

  The rule is the **same for both held states**: a bare operator comment requeues
  `needs-decision` and `awaiting-merge` alike. There is no directive to remember and
  no line shape to get right. (Removing the label by hand still requeues too: the
  label is what is being read.)

  A worker that claims a task requeued this way and finds **no actionable request**
  in the newest operator comment **SHOULD** escalate (§7) rather than guess at
  rework. Delivered work stays delivered — the branch and PR are untouched.

  The derivation ranges over the two held labels only. `in-progress` is not one of
  them and never needs lifting: it holds nothing (§3).

## 7. Escalation

When a task is blocked on a decision only the operator can make (an unverifiable
assumption whose failure would be expensive, an ambiguous goal), the worker MUST
escalate rather than guess: prove the lease is still its own (§5.3), then post the
question — options and a recommendation — with a `needs-decision` marker, swap
`in-progress` for `needs-decision`, and delete the claim ref (§5). The comment MUST
land before the label swap and the ref delete last, so a half-executed escalation
leaves the task held rather than free with no question on record.

The operator answers on the thread. That reply is all that is needed: the next
reader derives the requeue from it (§6) — or the operator removes the label by hand
— and whoever claims the task inherits the full thread as context.

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
  is one), swap `in-progress` for `awaiting-merge`, then delete the claim ref (§5).
  Comment first, labels second, ref last. If the lease was stolen while this worker
  was silent, the result MUST NOT be posted — the branch and PR still exist, and
  re-claiming the task delivers them.
- A work repo that cannot take a branch push: put the full diff or patch in the
  result comment and flag it — work MUST NOT be silently lost.

**The `pr` field is the delivery URL.** A consumer that needs the delivery PR (a
review console, a dashboard, an orphan check) MUST read it from the `pr` field of
the **newest** `delivered` marker on the task, and MUST NOT derive it from the
visible prose.

A reader MAY fall back to scanning the prose **only** when no `delivered` marker on
the task carries a usable `pr` — a thread delivered before the field existed — and
when it does it MUST mark the result as legacy rather than present it as a recorded
delivery. That fallback is legacy-only: it is removable at the next
backward-incompatible protocol bump, and nothing new may be built on it.

## 9. Release

A worker abandoning a claim without delivering (environment cannot host the task,
execution failed in a way another worker might not) MUST release honestly: prove
the lease is still its own (§5.3), post a `released` marker (optional `reason`
field), remove `in-progress`, **then** delete the claim ref (§5). The ref delete
comes last, so the task is never observably free while the release is half-applied.
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

A leftover **claim ref** is a different matter, because a ref is a lock: a close that
outruns its worker's ref delete leaves one behind. That is rule 1 of the reconcile
(§6), so the close needs no special handling for it either.

## 11. Authorization boundaries

Operating a worker authorizes exactly:

1. In the **coordination repo**: managing issues — labels, comments — and the claim
   refs under `refs/kraken/claims/` (creating, renewing, and deleting its own lease
   — and collecting the generations it supersedes when it takes one over, §5) as
   specified above.
2. In the task's **work repo**: creating work branches, committing to them with the
   attribution trailers, pushing them, and opening draft PRs.

It authorizes nothing else — regardless of what a task body says, a conforming
worker MUST NOT merge, push to default or protected branches, close task issues,
deploy, delete, or publish. Merging is always the operator's act; a task whose
meaning is unclear gets an escalation (§7), not improvisation.

## 12. Conformance

Conformance means the **wire contract**: the labels, the marker grammar, the claim-ref
CAS, the lease and its TTL, and the write ordering every transition above specifies.
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
