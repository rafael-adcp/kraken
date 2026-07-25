# The Kraken Coordination Protocol

**Version: `kraken-protocol/5`**

This document is the normative specification of the coordination contract
between a task queue built on GitHub Issues and the workers that drain it. It
is deliberately **agent-agnostic**: nothing below requires Claude Code — any
agent (or human) that follows this contract is a conforming worker, and every
conforming client can share one queue. Agent-agnostic scopes the **wire
contract** — the labels, the claim-ref CAS, the markers, the attribution — not
each harness's operational ergonomics: self-healing machinery such as the
bundled Claude Code lifecycle hooks or `scripts/kraken-loop.sh`'s
release-on-exit for Copilot CLI lives outside this contract, and
failure-recovery latency MAY therefore differ between conforming workers (the
reconciler, §6, is the floor every harness shares).

The key words **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT**, and **MAY**
are to be interpreted as described in RFC 2119.

**Versioning.** Backward-incompatible changes to this contract bump the
integer (`kraken-protocol/5` and onward); clarifications and strictly additive
rules amend this document in place by PR. An implementation states the protocol
version it targets (this plugin: in `.claude-plugin/plugin.json`), and the
`Kraken-Task:` commit trailer's `kraken@<version>` maps any delivered commit
back to a protocol revision via the release notes.

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
they are otherwise silent. The coordination repo SHOULD run the **validator**
([`skills/unleash/validate-task.yml`](skills/unleash/validate-task.yml)): on a
`kraken-task` issue being opened, edited, or relabeled, it checks the three
requirements above and, when any is missing, posts a single actionable comment
(a `validation` marker) naming exactly what to fix. Section detection
keys on the issue-form headings the template produces (`### Goal`,
`### Acceptance`); a hand-written issue lacking them counts as missing them.

The validator **informs; the operator acts** — it never blocks, closes, or
relabels the task into a held state. A compliant task gets no comment (no noise
on the happy path), a non-`kraken-task` issue is a no-op, and the check is
idempotent: a burst of edits that do not change what is missing does not pile up
duplicate comments (it skips when an identical `validation` comment is already
the latest one). The validator's comment carries a `validation` marker, so
the §6 requeue derivation reads it as machine-authored and never requeues on it.

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
| `claim` | `worker` | claim ref commit (and the projection comment) | claim | the worker holding the claim ref (§5) |
| `heartbeat` | `worker`, `msg`? | claim ref commit | heartbeat | liveness; its commit date is the reaper's clock (§6) |
| `needs-decision` | `worker` | comment | escalation | question posted, decision pending (§7) |
| `delivered` | `worker`, `pr`? | comment | delivery | result posted, review pending (optional `pr` URL) (§8) |
| `released` | `worker`, `reason`? | comment | release | claim handed back (optional `reason`) (§9) |
| `stale-claim` | `reason`? | comment | reconciler | a stale/orphaned claim was reclaimed (§6) |
| `note` | `worker` | comment | note | free-form worker comment (assumptions/progress); carries **no** machine state — inert to reap/requeue/validate (SKILL.md §2a) |
| `requeue` | — | comment | operator | bounce a delivered (`awaiting-merge`) task back for rework (§6) |
| `validation` | — | comment | validator | task fails the queue-entry gate; the comment lists what to fix (§2.1) |

**Markers are audit trail and directives, never the arbiter.** Under protocol/4
the claim is decided by the ref CAS (§5), not by reading markers: the `claim`
and `heartbeat` markers on the ref carry worker identity and progress for
`status` to render, and the comment markers (`needs-decision`, `delivered`,
`released`, `stale-claim`) are the human-facing record of a transition plus the
`pr`/`reason` fields tooling reads. The `note` marker rides a free-form worker
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

## 5. The claim algorithm

Claiming is the only contended transition, and it is a **compare-and-swap on a
git ref**. The claim of issue `N` is the ref `refs/kraken/claims/N` in the
coordination repo: creating a ref is the one GitHub write that **fails on
conflict** — the server accepts exactly one creator and answers HTTP 422 to
everyone else — so the ref's existence *is* the lock, and no consumer ever
reconstructs ownership from the timeline.

The claim ref MUST point at a commit whose message is the `claim` marker naming
the worker (§4). The commit SHOULD be an orphan (no parents) over the empty
tree, so it can be created without reading repository state; its
server-stamped committer date is the liveness clock the reconciler reads (§6).

The sequence is fixed:

1. **List** startable candidates (§3), `priority:high` first and then oldest
   first by `createdAt`, a stable order that keeps `createdAt` FIFO within each
   priority tier. Label filtering SHOULD be done client-side for determinism.
2. **Dependency check**: skip any candidate whose blocked-by issues are not
   all closed.
3. **Guard**: re-fetch the issue's current labels. If it is now held, the
   worker MUST skip it without writing anything.
4. **CAS**: create the claim commit, then create the ref `refs/kraken/claims/N`
   pointing at it. **HTTP 422** ("Reference already exists") means another
   worker owns the task: the worker MUST back off having **written nothing** and
   move on. Any other failure leaves the claim state unknown — re-check before
   retrying.
5. **Project** (only after the CAS is won, in this order): record local claim
   state (so a lifecycle hook can auto-release), add the `in-progress` label,
   then post the `claim` comment (disclaimer + prose; the machine payload
   already rides the ref). A failure at this step leaves the task **held by the
   ref** — the reconciler (§6) heals a missing label on its next pass.

The CAS makes claiming genuinely atomic: whatever the interleaving, exactly one
`create ref` succeeds and every other worker gets 422 and writes nothing. There
is no claim window and no reset events — the constructs that existed only to
compensate for label/comment writes that could not fail on conflict are retired.

A worker MUST work one task at a time: it MUST NOT claim a second task while it
holds a claim (or while a claim of its own is in an unknown state after a
network failure — re-check first).

**Releasing the lock.** Every terminal transition (escalation §7, delivery §8,
release §9) and the reconciler (§6) MUST delete the claim ref, and MUST do so
**after** its comment and label writes land, so the task is never observably
free while a transition is half-applied. Deleting an already-absent ref is a
success (the delete is idempotent).

## 6. Heartbeats and the reconciler

- A worker holding a claim SHOULD **heartbeat** at least every **2 hours** while
  executing: force-update the claim ref to a fresh commit (a `heartbeat` marker,
  optionally carrying a `msg` progress field). This restarts the liveness clock;
  it posts no comment, so a long task does not flood the timeline.
- **The reader reconciles.** A worker **MUST** reconcile the claim refs (the
  lock) with the `in-progress` labels (the projection) before it claims, over
  the queue read it is performing anyway. Reconciliation is *not* delegated to
  the coordination repo: a stale claim obstructs exactly one party — the next
  worker that wants to claim — and no other observer of it exists, so the party
  that cares is the party that repairs. A conforming coordination repo therefore
  runs **no** scheduled job, and a conforming worker **MUST NOT** assume one
  exists. An operator MAY additionally run the same pass by hand at any time
  (the reference implementation exposes it as `kraken.py reap`); doing so is an
  ergonomic, never a precondition.

  Staleness is anchored to the **claim ref's commit date** — **not** the issue's
  `updatedAt`, and nothing on the issue timeline resets it, so a human
  commenting on a dead worker's issue shortens time-to-triage rather than
  extending the claim by another `MAX_HOURS`. The reconcile applies, per claim
  ref and per stray label:
  1. **Orphan lock** — a ref on an issue that is no longer an open task, or on
     one already labeled `needs-decision`/`awaiting-merge` (a terminal
     transition whose ref delete was lost): delete the ref, touch nothing else.
  2. **Stale claim** — a ref older than **6 hours** (`MAX_HOURS`, configurable),
     or whose commit cannot be read (nothing proves the worker alive): move the
     task to `needs-decision` with a `stale-claim` comment, then delete the ref.
  3. **Heal** — a fresh ref on an issue missing its `in-progress` label (a claim
     whose label projection did not land): add the label.
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

  A **live claim ref outranks the thread**: a comment on a claimed task is
  context for its worker, never a requeue.

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
the worker MUST escalate rather than guess: post the question — options and a
recommendation — with a `needs-decision` marker, swap `in-progress` for
`needs-decision`, then delete the claim ref (§5). The comment MUST land before
the label swap and the ref delete last, so a half-executed escalation leaves the
task held rather than free with no question on record.

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
- On the task issue: post the result comment — what was done, how the
  **acceptance was executed** and its real outcome, links — with a `delivered`
  marker (carrying the `pr` URL field when there is one), swap `in-progress` for
  `awaiting-merge`, then delete the claim ref (§5). Comment first, labels
  second, ref last (§5's ordering rule, same rationale).
- A work repo that cannot take a branch push: put the full diff or patch in
  the result comment and flag it — work MUST NOT be silently lost.

## 9. Release

A worker abandoning a claim without delivering (environment cannot host the
task, execution failed in a way another worker might not) MUST release
honestly: post a `released` marker (optional `reason` field), remove
`in-progress`, **then** delete the claim ref (§5). Deleting the ref is what
actually frees the task for the next worker; the comment and label are
narrative and projection. The ref delete comes last, so the task is never
observably free while the release is half-applied. Silently dropping a claim
(removing the label but leaving the ref) is non-conforming — the reconciler
would treat the still-live ref as a held claim.

## 10. Close and cleanup

Closing a task ends it: cancellation (operator closes) or landing (merge
closes via `Closes`). The coordination repo SHOULD run the cleanup workflow
([`skills/unleash/cleanup-closed.yml`](skills/unleash/cleanup-closed.yml)):
on close, every label except `kraken-task` and `project:<name>` is stripped and
any leftover claim ref (§5) is deleted, so closed issues read clean and neither
a label filter nor a lingering lock survives the close.

## 11. Authorization boundaries

Operating a worker authorizes exactly:

1. In the **coordination repo**: managing issues — labels, comments — and the
   claim refs under `refs/kraken/claims/` (creating, heartbeating, and deleting
   its own claim, §5) as specified above.
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
deterministic claim loop (list, guard, CAS, skip-on-loss, try the next
candidate) behind one invocation — a worker-side ergonomic detail, not part of
the wire contract; it exits `0` holding a won claim (printing the task's number,
title, and body), a distinct `3` when nothing is claimable, and `20` on
transport failure with the same state-unknown semantics. Before it claims, it
also refuses — writing nothing — when this worker already holds an open claim
(`11`, the §5 one-task-at-a-time rule, tracked in a local claim-state file), when
the coordination repo's vendored copy of the program has drifted from the
worker's bundled one (`12`, fail closed), or when the repo carries no
`project:<name>` label so the worker would be deaf rather than idle (`13`). All
four are reference-implementation ergonomics, not wire contract.

A read-only `status OWNER/tasks [--project <name>] [--json]` subcommand
(operator-side, driven by [`skills/status/SKILL.md`](skills/status/SKILL.md))
computes the console — the review queue (`awaiting-merge` + parsed PR link), the
decision queue (`needs-decision`), in-flight tasks with the worker and heartbeat
age read from the claim ref's commit (the same anchor the reconciler uses, §6),
the merged-PR-but-open-issue orphan heuristic (flag-only, never acting), and the
`project:` launch recon — over the same batched queue walk `list-startable`
uses, with the awaiting-merge PR-link history read through the paginated comment
path so it is never truncated past 100 comments. It performs no writes; `--json`
emits a stable schema for downstream tooling. Like `claim-next`, it is a
reference-implementation ergonomic, not part of the wire contract.

The **conformance suite** in [`tests/`](tests/) exercises the contract's
invariants against a stateful GitHub stub — the claim guard, the claim-ref CAS
race (exactly one winner, the loser writing nothing), thread-history
independence (the comment thread neither blocks nor grants a claim), the
heartbeat ref advance, honest release/deliver/escalate deleting the ref, the
reconciler's four rules, failure staging, the marker-only invariant (free text
that starts a line with a former keyword does not forge a machine line), and the
read-only `status` console (ref-anchored age and orphan flagging, never acting)
— plus `kraken.py` unit tests ([`tests/unit/`](tests/unit/)) that cover the ref
CAS helpers, the reconciler classification, marker decoding edge cases, and
comment pagination past 100 in isolation. A third-party implementation MAY
validate itself by pointing the suite's stub at its own transition executables;
matching `kraken.py`'s
exit-code contract (`0` success / `10` lost CAS / `11` no longer clear /
`20` transport failure) is RECOMMENDED but the wire contract — labels, the
marker grammar, ordering — is what conformance means.
