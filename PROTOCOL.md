# The Kraken Coordination Protocol

**Version: `kraken-protocol/3`**

This document is the normative specification of the coordination contract
between a task queue built on GitHub Issues and the workers that drain it. It
is deliberately **agent-agnostic**: nothing below requires Claude Code — any
agent (or human) that follows this contract is a conforming worker, and every
conforming client can share one queue.

The key words **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT**, and **MAY**
are to be interpreted as described in RFC 2119.

**Versioning.** Backward-incompatible changes to this contract bump the
integer (`kraken-protocol/3` and onward); clarifications and strictly additive
rules amend this document in place by PR. An implementation states the protocol
version it targets (this plugin: in `.claude-plugin/plugin.json`), and the
`Kraken-Task:` commit trailer's `kraken@<version>` maps any delivered commit
back to a protocol revision via the release notes.

**What changed in `kraken-protocol/3`.** The retired protocol/1 **visible line
grammar** (`^<keyword>: <value>` scanned per line) is no longer read at all.
protocol/2 required consumers to dual-read both the hidden marker and the
legacy line grammar so pre-existing threads kept arbitrating; protocol/3 drops
that requirement — a conforming consumer reads **only** the hidden marker (§4).
This is a backward-incompatible consumer change (an unmigrated protocol/1-only
thread no longer arbitrates), so it bumps the integer. The upside is a stronger
invariant: because no consumer parses visible prose as a machine line, free
text in a comment (a result file, a question, a heartbeat message, a release
reason) **cannot** forge a machine line — the entire prefix-collision fragility
class is gone structurally, not by escaping. The **semantics are unchanged** —
the claim window, its reset events, first-claim-wins, and heartbeat-never-resets
all behave exactly as in protocol/2.

**What changed in `kraken-protocol/2`.** Machine payloads moved from the
visible line grammar of protocol/1 (`^<keyword>: <value>` scanned per line) to
a structured **hidden marker** — an HTML comment carrying JSON (§4). This
retired three fragilities of the line grammar: prefix-scanning every comment
line (with its CRLF/quoting hazard class), accidental human collisions (a
comment that happens to start a line with a keyword), and reconstructing claim
state from interleaved free text. The visible prose in a marker-carrying
comment is a human-facing courtesy and MUST NOT be machine-parsed.

---

## 1. Actors and repositories

| Term | Meaning |
| --- | --- |
| **Operator** | The human who owns the queue: files tasks, answers decisions, merges PRs. |
| **Worker** | A named agent session draining the queue, one task at a time, inside one prepared environment. |
| **Coordination repo** | A GitHub repository whose **Issues are the queue**. It also holds the state-machine labels, the reaper and cleanup workflows, and the dependency graph. It MUST be private and MUST NOT hold work code. |
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
the latest one). The validator's comment is authored by the Actions bot, so the
requeue workflow's Bot gate (§6) ignores it.

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
Colors and label descriptions are SHOULD (they make every kraken queue read
the same — see `skills/init/SKILL.md`); the label *names* are MUST.

A task is **held** when it carries `in-progress`, `needs-decision`, or
`awaiting-merge`; it is **startable** when it is open, labeled `kraken-task` +
`project:<name>`, not held, and every blocked-by issue is closed. A task MUST
carry at most one held label at a time — stacking them (e.g. `in-progress` +
`awaiting-merge`) is state corruption, and the claim guard (§5) exists to
prevent it.

Legal transitions and who performs them:

| Transition | Actor | Mechanism |
| --- | --- | --- |
| queued → `in-progress` | worker | claim (§5) |
| `in-progress` → `needs-decision` | worker | escalation (§7) |
| `in-progress` → `needs-decision` | reaper | staleness (§6) |
| `in-progress` → `awaiting-merge` | worker | delivery (§8) |
| `in-progress` → queued | worker | release (§9) |
| `needs-decision` → queued | operator | reply on the thread — the requeue workflow (§6) drops the label; or remove it by hand |
| `needs-decision` → queued | requeue workflow | a non-tentacle comment arrives (§4 disclaimer absent) |
| `awaiting-merge` → queued | operator | review feedback on the thread, then remove the label (a bare comment does NOT requeue — see §6) |
| `awaiting-merge` → closed | merge | the PR's `Closes` reference (§8), or a manual close |

Workers MUST NOT close task issues: "done" for a worker means *delivered for
review*, and the task closes when the work truly lands. Closing an issue is
cancellation (operator) or landing (merge).

## 4. Comments: the machine marker and attribution

State-changing comments carry their machine payload in exactly one **hidden
marker**: an HTML comment carrying a compact single-line JSON object, of the
form

```
<!-- kraken {"type":"claim","worker":"env-1"} -->
```

The marker is invisible in GitHub's rendered timeline, so the surrounding
prose is a pure human courtesy. **Grammar** (normative):

- The marker opens with the literal delimiter `<!-- kraken ` (the token
  `kraken`, then a space), followed by the JSON object, then the literal
  closing delimiter ` -->`. Consumers SHOULD match it as
  `<!--\s*kraken\s+(\{…\})\s*-->`.
- The JSON object MUST be a single line (no embedded newline) and MUST carry a
  string **`type`** field naming the transition. Producers MUST encode it with
  a real JSON serializer (never string interpolation) — this is what retires
  the CRLF/quoting hazard class. Producers SHOULD emit ASCII-only JSON so the
  marker never carries a byte a `C`-locale filter could miss.
- A state-changing comment MUST carry exactly one marker. Consumers MUST scan
  every comment in server order and decode each marker; a malformed marker
  (undecodable JSON, no string `type`) MUST be ignored, never guessed.

| `type` | Fields | Posted by | Meaning | Claim-window reset (§5)? |
| --- | --- | --- | --- | --- |
| `claim` | `worker` | claim | this worker claims the task | — (it is the claim) |
| `heartbeat` | `worker` | heartbeat | liveness; resets the reaper clock only | **No** — MUST NOT reset |
| `needs-decision` | `worker` | escalation | question posted, decision pending | Yes |
| `delivered` | `worker`, `pr`? | delivery | result posted, review pending (optional `pr` URL) | Yes |
| `released` | `worker`, `reason`? | release | claim handed back (optional `reason`) | Yes |
| `stale-claim` | `reason`? | reaper | claim reclaimed from a silent worker | Yes |
| `requeue` | — | operator | bounce a delivered (`awaiting-merge`) task back for rework (§6) | n/a (operator directive) |
| `validation` | — | validator | task fails the queue-entry gate; the comment lists what to fix (§2.1) | n/a (never touches a claim) |

**Reading (markers only).** A conforming protocol/3 consumer reads machine
state from the hidden marker and **nothing else**: it MUST NOT parse the visible
prose of a comment as a machine line. The retired protocol/1 line grammar
(`^<keyword>: <value>`) is no longer read, so a line of free text that happens
to begin with `released:`, `delivered:`, `claimed-by:`, `heartbeat:`, or any
other former keyword is inert — it can never occupy a machine-line position.
Producers MUST carry every machine payload in a marker and MUST NOT rely on the
prose being parsed.

Every worker-posted coordination-repo comment MUST open with the
**attribution disclaimer** — every worker may authenticate as the operator,
so the disclaimer is what lets the timeline distinguish tentacle comments
from human ones:

```
> 🐙 **Kraken worker `<worker-name>`** — automated comment from a Claude Code tentacle, not a human.
```

(Non–Claude Code implementations substitute their own tool name; the
blockquote + worker name shape is the contract.) The disclaimer sits *above*
the prose and marker with a blank line between, or GitHub folds the body into
the quote. The block above is **illustrative**: the Kraken reference
implementation defines the format once as the `DISCLAIMER` constant in
`skills/unleash/kraken.py` and every other occurrence derives from it —
`kraken.py contract disclaimer` prints the authoritative line, and consumers
that must recognize a worker comment (the `requeue-on-reply.yml` filter) are
verified against it by executing both rather than by copying the literal.

## 5. The claim algorithm

Claiming is the only contended transition; its sequence is fixed:

1. **List** startable candidates (§3), oldest first by `createdAt`. Label
   filtering SHOULD be done client-side for determinism.
2. **Dependency check**: skip any candidate whose blocked-by issues are not
   all closed.
3. **Guard**: re-fetch the issue's current labels. If it is now held, the
   worker MUST skip it without writing anything — never stack a held label.
4. **Label**: add `in-progress`.
5. **Comment**: post a `claim` marker (with the disclaimer) —
   `<!-- kraken {"type":"claim","worker":"<worker>"} -->`.
6. **Arbitrate**: re-read the comments. The winner is the **first `claim` of
   the current claim window**, in server-side comment order — server ordering
   is the tiebreaker between two workers that passed the guard at the same
   instant. If the winner is not this worker, it MUST back off **removing
   nothing** (the winner owns the label and the claim) and move on.

**The claim window** starts immediately after the most recent reset marker —
`released` / `stale-claim` / `needs-decision` / `delivered` — in the
comment stream (or at the beginning of the thread if none exists). `claim`
markers before that point MUST be ignored during arbitration — otherwise a
task once claimed by a dead worker, or delivered and bounced back by review,
could never be claimed again (or only by its original worker). A `heartbeat`
MUST NOT reset the window: a worker signalling liveness must never make its
own claim re-claimable.

GitHub offers no compare-and-swap on labels, so this algorithm does not make
claiming atomic; it makes the race **safe**: whatever the interleaving,
arbitration yields exactly one winner and losers write nothing further.

A worker MUST work one task at a time: it MUST NOT claim a second task while
it holds a claim (or while a claim of its own is in an unknown state after a
network failure — re-check first).

## 6. Heartbeats and the reaper

- A worker holding `in-progress` SHOULD post a heartbeat (a progress comment;
  a `heartbeat` marker) at least every **2 hours** while executing.
- The coordination repo runs the **reaper**
  ([`skills/unleash/reclaim-stale.yml`](skills/unleash/reclaim-stale.yml)):
  any `in-progress` issue whose worker has been silent for **6 hours**
  (`MAX_HOURS`, configurable) is moved to `needs-decision` with a
  `stale-claim` marker for the operator to triage. Staleness is anchored to
  the worker's **last liveness marker** — the most recent `claim` /
  `heartbeat` comment —
  **not** the issue's `updatedAt`. Operator comments and other activity do
  **not** reset the clock: a human commenting on a dead worker's issue must
  shorten time-to-triage, not extend the claim's life by another `MAX_HOURS`.
  Only the worker's own `heartbeat` keeps a live claim alive. An `in-progress`
  issue with no `claim`/`heartbeat` marker at all is treated as infinitely
  stale and reclaimed. (`delivered` / `released` already remove `in-progress`,
  so they never anchor a still-held claim.)
- The coordination repo also runs the **requeue-on-reply** workflow
  ([`skills/unleash/requeue-on-reply.yml`](skills/unleash/requeue-on-reply.yml)):
  on a new comment, it removes the holding label so the task requeues — so the
  operator's gesture collapses from "reply **and** remove the label" to just
  "reply". The human-vs-tentacle discriminator is §4's attribution disclaimer:
  a comment that does **not** open with the `> 🐙 **Kraken worker …`
  blockquote is treated as the operator. It is a **no-op** on worker comments
  (disclaimer present), on issues carrying no held label, and on bot/self
  comments (`user.type == Bot`) — which is what keeps the reaper's own
  `stale-claim` comment from instantly undoing the escalation it just posted.
  The two held states are handled asymmetrically: a bare operator comment
  requeues **`needs-decision`** (a human comment is almost always the answer;
  a "let me think" self-corrects via re-escalation), but **`awaiting-merge`**
  is already *delivered* and is left held unless the comment carries an
  explicit, structured **requeue directive** — either a
  `<!-- kraken {"type":"requeue"} -->` marker or a standalone `requeue:` line
  (a line whose only content is `requeue`/`requeue:`). A `requeue:` buried in
  a prose sentence MUST NOT bounce a ready branch back to a worker.
  Requeuing is idempotent, so a burst of comments requeues once (the first
  drops the label; the rest find nothing held).

## 7. Escalation

When a task is blocked on a decision only the operator can make (an
unverifiable assumption whose failure would be expensive, an ambiguous goal),
the worker MUST escalate rather than guess: post the question — options and a
recommendation — with a `needs-decision` marker, then swap `in-progress` for
`needs-decision`. The comment MUST land before the label swap, so a
half-executed escalation leaves the task held rather than re-claimable with a
closed window.

The operator answers on the thread; the requeue-on-reply workflow (§6) removes
the label (or the operator removes it by hand), the task requeues, and whoever
claims it inherits the full thread as context.

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
  marker (carrying the `pr` URL field when there is one), then swap
  `in-progress` for `awaiting-merge`. Comment first, labels second (§7's
  ordering rule, same rationale).
- A work repo that cannot take a branch push: put the full diff or patch in
  the result comment and flag it — work MUST NOT be silently lost.

## 9. Release

A worker abandoning a claim without delivering (environment cannot host the
task, execution failed in a way another worker might not) MUST release
honestly: post a `released` marker (optional `reason` field), **then** remove
`in-progress`. The comment MUST land first — the `released` marker is what
closes the claim window, so removing the label alone would leave the old
`claim` winning every future arbitration. Silently dropping a claim (removing
the label with no `released` marker) is non-conforming.

## 10. Close and cleanup

Closing a task ends it: cancellation (operator closes) or landing (merge
closes via `Closes`). The coordination repo SHOULD run the cleanup workflow
([`skills/unleash/cleanup-closed.yml`](skills/unleash/cleanup-closed.yml)):
on close, every label except `kraken-task` and `project:<name>` is stripped,
so closed issues read clean and label-based queue filters never match dead
state.

## 11. Authorization boundaries

Operating a worker authorizes exactly:

1. In the **coordination repo**: managing issues — labels, comments — as
   specified above.
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
with a subcommand per transition (`list-startable`, `claim`, `heartbeat`,
`escalate`, `deliver`, `release`, `watch`), driven by a Claude Code skill
([`skills/unleash/SKILL.md`](skills/unleash/SKILL.md)) that supplies the
judgment between transitions. `gh` remains the transport, so it runs against any
authenticated `gh`. It also ships a `claim-next OWNER/tasks <project> <worker>`
convenience that composes `list-startable` + `claim` into the whole
deterministic claim loop (list, guard, label, comment, arbitrate, skip-on-loss,
try the next candidate) behind one invocation — a worker-side ergonomic detail,
not part of the wire contract; it exits `0` holding a won claim (printing the
task's number, title, and body), a distinct `3` when nothing is claimable, and
`20` on transport failure with the same state-unknown semantics.

A read-only `status OWNER/tasks [--project <name>] [--json]` subcommand
(operator-side, driven by [`skills/status/SKILL.md`](skills/status/SKILL.md))
computes the console — the review queue (`awaiting-merge` + parsed PR link), the
decision queue (`needs-decision`), in-flight tasks with a heartbeat age anchored
to the worker's last liveness marker (the same anchor the reaper uses, §11), the
merged-PR-but-open-issue orphan heuristic (flag-only, never acting), and the
`project:` launch recon — over the same batched queue walk `list-startable`
uses, with the heartbeat/PR-link history read through the paginated comment path
so it is never truncated past 100 comments. It performs no writes; `--json`
emits a stable schema for downstream tooling. Like `claim-next`, it is a
reference-implementation ergonomic, not part of the wire contract.

The **conformance suite** in [`tests/`](tests/) exercises the contract's
invariants against a stateful GitHub stub — the claim guard, the claim race
(exactly one winner), claim-window arbitration including the review-bounce
reset, honest release, failure staging, the marker-only arbitration invariant
(free text that starts a line with a former keyword does not forge a machine
line), and the read-only `status` console (heartbeat-age anchoring and orphan
flagging, never acting) — plus `kraken.py` unit tests
([`tests/unit/`](tests/unit/)) that cover marker arbitration,
marker decoding edge cases, and comment pagination past 100 in
isolation. A third-party implementation MAY validate itself by pointing the
suite's stub at its own transition executables; matching `kraken.py`'s
exit-code contract (`0` success / `10` lost tiebreaker / `11` no longer clear /
`20` transport failure) is RECOMMENDED but the wire contract — labels, the
marker grammar, ordering — is what conformance means.
