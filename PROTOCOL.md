# The Kraken Coordination Protocol

**Version: `kraken-protocol/1`**

This document is the normative specification of the coordination contract
between a task queue built on GitHub Issues and the workers that drain it. It
is deliberately **agent-agnostic**: nothing below requires Claude Code — any
agent (or human) that follows this contract is a conforming worker, and every
conforming client can share one queue.

The key words **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT**, and **MAY**
are to be interpreted as described in RFC 2119.

**Versioning.** Backward-incompatible changes to this contract bump the
integer (`kraken-protocol/2`); clarifications and strictly additive rules
amend this document in place by PR. An implementation states the protocol
version it targets (this plugin: in `.claude-plugin/plugin.json`), and the
`Kraken-Task:` commit trailer's `kraken@<version>` maps any delivered commit
back to a protocol revision via the release notes.

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
the **worker name** carried by machine lines and trailers (§4), never in the
authenticating account, and assignees MUST NOT be used to arbitrate anything.

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

## 4. Comments: machine lines and attribution

State-changing comments carry a **machine line**: a line matching
`^<keyword>: <value>` inside the comment body. Consumers MUST scan all
comment lines in server order; producers MUST put each machine line on its
own line.

| Machine line | Posted by | Meaning | Claim-window reset (§5)? |
| --- | --- | --- | --- |
| `claimed-by: <worker>` | claim | this worker claims the task | — (it is the claim) |
| `heartbeat: <worker>` | heartbeat | liveness; resets the reaper clock only | **No** — MUST NOT reset |
| `needs-decision: <worker>` | escalation | question posted, decision pending | Yes |
| `delivered: <worker>` | delivery | result posted, review pending | Yes |
| `pr: <url>` | delivery | the draft PR/MR under review | n/a (companion line) |
| `released: <worker>` | release | claim handed back | Yes |
| `reason: <text>` | release | why (optional companion line) | n/a |
| `stale-claim: <details>` | reaper | claim reclaimed from a silent worker | Yes |

Every worker-posted coordination-repo comment MUST open with the
**attribution disclaimer** — every worker may authenticate as the operator,
so the disclaimer is what lets the timeline distinguish tentacle comments
from human ones:

```
> 🐙 **Kraken worker `<worker-name>`** — automated comment from a Claude Code tentacle, not a human.
```

(Non–Claude Code implementations substitute their own tool name; the
blockquote + worker name shape is the contract.) The disclaimer sits *above*
the machine line with a blank line between the two, or GitHub folds the body
into the quote.

## 5. The claim algorithm

Claiming is the only contended transition; its sequence is fixed:

1. **List** startable candidates (§3), oldest first by `createdAt`. Label
   filtering SHOULD be done client-side for determinism.
2. **Dependency check**: skip any candidate whose blocked-by issues are not
   all closed.
3. **Guard**: re-fetch the issue's current labels. If it is now held, the
   worker MUST skip it without writing anything — never stack a held label.
4. **Label**: add `in-progress`.
5. **Comment**: post `claimed-by: <worker>` (with the disclaimer).
6. **Arbitrate**: re-read the comments. The winner is the **first
   `claimed-by:` line of the current claim window**, in server-side comment
   order — server ordering is the tiebreaker between two workers that passed
   the guard at the same instant. If the winner is not this worker, it MUST
   back off **removing nothing** (the winner owns the label and the claim)
   and move on.

**The claim window** starts immediately after the most recent
`released:` / `stale-claim:` / `needs-decision:` / `delivered:` machine line
in the comment stream (or at the beginning of the thread if none exists).
`claimed-by:` lines before that point MUST be ignored during arbitration —
otherwise a task once claimed by a dead worker, or delivered and bounced back
by review, could never be claimed again (or only by its original worker).
`heartbeat:` MUST NOT reset the window: a worker signalling liveness must
never make its own claim re-claimable.

GitHub offers no compare-and-swap on labels, so this algorithm does not make
claiming atomic; it makes the race **safe**: whatever the interleaving,
arbitration yields exactly one winner and losers write nothing further.

A worker MUST work one task at a time: it MUST NOT claim a second task while
it holds a claim (or while a claim of its own is in an unknown state after a
network failure — re-check first).

## 6. Heartbeats and the reaper

- A worker holding `in-progress` SHOULD post a heartbeat (a progress comment;
  the `heartbeat:` machine line) at least every **2 hours** while executing.
- The coordination repo runs the **reaper**
  ([`skills/unleash/reclaim-stale.yml`](skills/unleash/reclaim-stale.yml)):
  any `in-progress` issue whose worker has been silent for **6 hours**
  (`MAX_HOURS`, configurable) is moved to `needs-decision` with a
  `stale-claim:` comment for the operator to triage. Staleness is anchored to
  the worker's **last machine line** — the most recent `claimed-by:` /
  `heartbeat:` comment — **not** the issue's `updatedAt`. Operator comments
  and other activity do **not** reset the clock: a human commenting on a dead
  worker's issue must shorten time-to-triage, not extend the claim's life by
  another `MAX_HOURS`. Only the worker's own `heartbeat:` keeps a live claim
  alive. An `in-progress` issue with no `claimed-by:`/`heartbeat:` line at all
  is treated as infinitely stale and reclaimed. (`delivered:` / `released:`
  already remove `in-progress`, so they never anchor a still-held claim.)
- The coordination repo also runs the **requeue-on-reply** workflow
  ([`skills/unleash/requeue-on-reply.yml`](skills/unleash/requeue-on-reply.yml)):
  on a new comment, it removes the holding label so the task requeues — so the
  operator's gesture collapses from "reply **and** remove the label" to just
  "reply". The human-vs-tentacle discriminator is §4's attribution disclaimer:
  a comment that does **not** open with the `> 🐙 **Kraken worker …`
  blockquote is treated as the operator. It is a **no-op** on worker comments
  (disclaimer present), on issues carrying no held label, and on bot/self
  comments (`user.type == Bot`) — which is what keeps the reaper's own
  `stale-claim:` comment from instantly undoing the escalation it just posted.
  The two held states are handled asymmetrically: a bare operator comment
  requeues **`needs-decision`** (a human comment is almost always the answer;
  a "let me think" self-corrects via re-escalation), but **`awaiting-merge`**
  is already *delivered* and is left held unless the comment carries an
  explicit `requeue:` line — a bare comment there would bounce a ready branch
  back to a worker. Requeuing is idempotent, so a burst of comments requeues
  once (the first drops the label; the rest find nothing held).

## 7. Escalation

When a task is blocked on a decision only the operator can make (an
unverifiable assumption whose failure would be expensive, an ambiguous goal),
the worker MUST escalate rather than guess: post the question — options and a
recommendation — with the `needs-decision: <worker>` machine line, then swap
`in-progress` for `needs-decision`. The comment MUST land before the label
swap, so a half-executed escalation leaves the task held rather than
re-claimable with a closed window.

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
  **acceptance was executed** and its real outcome, links — with the
  `delivered: <worker>` and `pr: <url>` machine lines, then swap
  `in-progress` for `awaiting-merge`. Comment first, labels second (§7's
  ordering rule, same rationale).
- A work repo that cannot take a branch push: put the full diff or patch in
  the result comment and flag it — work MUST NOT be silently lost.

## 9. Release

A worker abandoning a claim without delivering (environment cannot host the
task, execution failed in a way another worker might not) MUST release
honestly: post `released: <worker>` (optional `reason:` line), **then**
remove `in-progress`. The comment MUST land first — the `released:` line is
what closes the claim window, so removing the label alone would leave the old
`claimed-by:` winning every future arbitration. Silently dropping a claim
(removing the label with no `released:` line) is non-conforming.

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
authenticated `gh`. The bundled `*.sh` files next to it are thin shims that
`exec` into `kraken.py`, preserving the historical entry points.

The **conformance suite** in [`tests/`](tests/) exercises the contract's
invariants against a stateful GitHub stub — the claim guard, the claim race
(exactly one winner), claim-window arbitration including the review-bounce
reset, honest release, and failure staging — plus `kraken.py` unit tests
([`tests/unit/`](tests/unit/)) that cover the arbitration grammar, machine-line
parsing, and comment pagination past 100 in isolation. A third-party
implementation MAY validate itself by pointing the suite's stub at its own
transition executables; matching `kraken.py`'s exit-code contract
(`0` success / `10` lost tiebreaker / `11` no longer clear / `20` transport
failure) is RECOMMENDED but the wire contract — labels, machine lines,
ordering — is what conformance means.
