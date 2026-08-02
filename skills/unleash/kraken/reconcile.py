"""The §6 repair pass: what to fix, and applying it.

Part of the kraken protocol package; see __init__.py."""
from __future__ import annotations

import argparse
import sys
from typing import Any, Callable, Mapping, Sequence

from .contract import (
    EXIT_OK, EXIT_TRANSPORT, Issue, Json, ReconcileAction, Worker, diag
)
from .comments import compose_comment
from .transport import Api
from .lease import LEASE_EXPIRY_ESCALATE, Lease
from .refs import Refs
from .state import NO_RECORD, States, TaskState
from .queue import Queue, Task

# --- coordination-repo subcommands -------------------------------------------
# The logic-bearing coordination passes (reconcile, validate-task) live here
# rather than re-implementing the protocol parse in jq/grep/awk — ONE parser,
# sharing the marker decoder, disclaimer, and label vocabulary (and unit tests)
# with the worker side.

# --- the reconciler (PROTOCOL.md §6) -----------------------------------------
# The claim ref is the lease and its commit date the lease timestamp. The READER
# reconciles it, not a cron in the coordination repo: a dead worker's lease
# obstructs exactly one party — the next worker who wants to claim — and there is
# no other observer of it, so the reconcile rides the claim path (cmd_claim_next)
# on the queue read it already paid for. `reap` keeps the same pass as an
# operator-side escape hatch; both go through the pure planner below, so the two
# entry points can never drift.
#
# The pass repairs exactly what the leases cannot fix on their own: a lock over a
# task that has moved on, and a task that keeps expiring. An expired lease is not
# among them — it is not a repair, it is simply not held, and the claim steals it
# (§5) at no write.

# Who a stand-alone `reap` attributes its comments to when the operator names no
# worker. A drain passes its own worker name instead: the reconciler's comments
# are posted by a worker's token, so they carry the §4 attribution disclaimer
# like every other worker comment.
RECONCILER_WORKER = "reconciler"


def stale_claim_body(worker: Worker, reason: str) -> str:
    """The reconciler's reclaim comment: the attribution disclaimer, human prose,
    and the stale-claim marker (audit trail). The disclaimer is required because
    a worker's token posts this, like every other worker comment (§4)."""
    prose = (
        f"Nobody is finishing this task ({reason}). Stealing the lease again "
        "would just burn another worker, so it needs a human call. To requeue, "
        "reply on this thread — or remove the needs-decision label by hand."
    )
    return compose_comment(
        worker, prose, {"type": "stale-claim", "reason": reason}
    )


def reconcile_plan(
    tasks: Sequence[Task], leases: dict[Issue, Lease],
    states: Mapping[Issue, TaskState] | None = None,
    max_expiries: int = LEASE_EXPIRY_ESCALATE,
) -> list[ReconcileAction]:
    """The reconciler's decision as a PURE function of one queue read — no
    network, so every rule is unit-testable in isolation (PROTOCOL.md §6). The
    expiry itself is already decided (`lease_state`); these are the repairs the
    expiry does NOT make on its own:

      1. **orphan ref** — a claim ref or a state record whose issue is not an
         open task any more, or a claim ref on a task whose record already names
         a held state (a terminal transition whose ref delete was lost): delete
         that ref, touch nothing else;
      2. **reclaim** — a task whose record reports `expiries` ≥ `max_expiries`:
         move it to needs-decision with a stale-claim comment, write the record,
         then delete the ref. This is the ONLY thing an expiry escalates. A task
         that kills whatever worker touches it — a poisoned environment, a step
         that always hangs — would otherwise be stolen, dropped, stolen again for
         as long as the queue has workers, each round burning a drain and telling
         nobody. Anything below the threshold is not the reconciler's business:
         the lease is simply not held, and the claim path steals it;
      3. **migrate** — an open task wearing a held label with NO record: write
         the record its label already implies, touching no label, comment or ref.
         This is what carries a queue written before protocol/9 across the
         upgrade, and what lets this reader tolerate a writer that still only
         sets labels. It runs once per task, because after it the record exists;
      4. **re-anchor** — an open task whose held record's `comments` anchor has
         outrun its own thread (comments were deleted): move the anchor back
         onto the live total, lifting nothing. §6's derivation is memoryless by
         design — it compares two integers and cannot tell that one of them is
         now unreachable — so without this repair a deletion buries every reply
         that follows it, and the operator sits watching a task they answered.
         The hold is preserved: a deleted comment is not an answer.

    Rule 4 is a PROPOSAL, not a verdict: the applier confirms the shrink against
    the same surface a transition anchors from before writing anything. The walk
    reports a count it may have failed to select (`_comment_total` floors that
    to 0), and a repair that trusted it would re-anchor the whole queue to zero.

    No rule is about the in-progress LABEL: that label is write-only (§3), so a
    wrong badge misleads nobody and misroutes nothing — it is not a repair the
    reader owes anyone, and the next transition on the task overwrites it anyway.

    `tasks` is the open-kraken-task walk (repo-wide, before any project filter),
    so rule 1 needs no per-ref issue read: a ref on an issue absent from that
    walk is a ref on an issue that is closed, or no longer a task at all —
    either way it holds a lock, or a state, over nothing. Nothing on the issue
    timeline anchors liveness, so an operator poking a dead worker's thread
    shortens time-to-triage rather than extending the lease.

    Returns a list of `{"rule", "issue", "reason"}` actions ordered by rule then
    issue number; an empty list means every lease is one a worker is entitled to
    hold and every held task has said so in a record (the overwhelmingly common
    case, and it costs zero writes)."""
    states = {} if states is None else states
    by_number = {task.number: task for task in tasks}
    plan = []

    # Rules 1 and 2 are keyed on a claim ref, so the walk is over the leases — a
    # task with no ref has no lock for this pass to repair, whatever labels it
    # wears.
    for num in sorted(leases):
        task = by_number.get(num)
        record = states.get(num, NO_RECORD)
        if task is None or record.holds(task.comment_total):
            plan.append({"rule": "orphan-lock", "issue": num,
                         "reason": "the task already left the claim",
                         "gens": list(leases[num].gens)})
            continue

        if leases[num].expired and record.expiries >= max_expiries:
            plan.append({"rule": "reclaim", "issue": num,
                         # The escalation writes needs-decision, and clears
                         # the in-progress badge if the task still wears one:
                         # write-only does not mean write-and-forget.
                         "reason": f"the lease expired {record.expiries} times "
                                   "and no worker has finished the task",
                         "held": "in-progress" in task.labels,
                         "gens": list(leases[num].gens)})
        # Below the threshold: not held, not repaired — stolen by the claim.

    # A state record on an issue the walk no longer carries is state over
    # nothing, the same orphan rule 1 applies to a lock. Deleting it keeps the
    # namespace from growing one ref per task the queue has ever closed.
    walked = set(by_number)
    for num in sorted(states):
        if num not in walked:
            plan.append({"rule": "orphan-state", "issue": num,
                         "reason": "the task is no longer an open task"})

    # Rules 3 and 4 are keyed on the record — its absence and its anchor — so
    # they walk the tasks. They cannot both fire on one task: a task with no
    # record has no anchor to have outrun anything.
    for num in sorted(by_number):
        task = by_number[num]
        record = states.get(num)
        if record is None:
            if task.held:
                plan.append({"rule": "migrate", "issue": num,
                             "reason": "held by a label with no state record",
                             "state": task.held[0],
                             "comments": task.comment_total})
        elif record.held_state and task.comment_total < record.comments:
            plan.append({"rule": "re-anchor", "issue": num,
                         "reason": f"the record anchors at {record.comments} "
                                   f"comments and the thread carries "
                                   f"{task.comment_total}"})

    return plan


def _reconcile_failure(stage: str, issue: Issue) -> None:
    """One failure shape for the applier: name the stage and the issue on stderr,
    then answer None so the caller surfaces exit 20."""
    print(f"reap: gh-failure stage={stage} issue={issue}", file=sys.stderr)
    return None


def apply_reconcile(api: Api, plan: Sequence[ReconcileAction],
                    worker: Worker) -> dict[str, int] | None:
    """Execute a reconcile plan. Returns per-rule counts, or None on the first
    transport failure. A half-applied pass is safe: every rule is idempotent and
    ends with the ref delete, so a crash leaves the task HELD and the next
    reader's rule 1 finishes the job — the task is never observably free while a
    reclaim is half-applied (§5's ordering rule)."""
    counts = {"orphan-lock": 0, "orphan-state": 0, "reclaim": 0, "migrate": 0,
              "re-anchor": 0}
    states = States(api)
    for action in plan:
        rule, num = action["rule"], action["issue"]

        if rule == "orphan-lock":
            if not Refs(api).drop(num, action["gens"]):
                return _reconcile_failure("ref", num)
            diag(f"reap: orphan-lock issue={num} — claim ref deleted")

        elif rule == "orphan-state":
            if not states.delete(num):
                return _reconcile_failure("state", num)
            diag(f"reap: orphan-state issue={num} — state record deleted")

        elif rule == "reclaim":
            if not api.post_comment(
                    num, stale_claim_body(worker, action["reason"])):
                return _reconcile_failure("comment", num)
            # Comment, then RECORD, then labels, then the ref (§3.1): the record
            # has to be on the server before the lock that holds the task goes,
            # or the task is observably queued while it is waiting on a human.
            total = api.comment_count(num)
            if total is None:
                return _reconcile_failure("count", num)
            if not states.write(num, states.of(num).moved_to(
                    "needs-decision", worker, total)):
                return _reconcile_failure("state", num)
            if not api.swap_labels(
                num,
                remove="in-progress" if action["held"] else None,
                add="needs-decision",
            ):
                return _reconcile_failure("labels", num)
            if not Refs(api).drop(num, action["gens"]):
                return _reconcile_failure("ref", num)
            diag(f"reap: reclaimed issue={num} ({action['reason']})")

        elif rule == "migrate":
            # The label is already there and already holding — this only writes
            # down what it means, so it posts nothing and swaps nothing. The
            # count is the one the walk observed: any comment after it requeues,
            # which is the correct reading of "nothing has been said since we
            # learned about this task".
            if not states.write(num, TaskState(
                    state=action["state"], worker=worker,
                    comments=action["comments"], recorded=True)):
                return _reconcile_failure("state", num)
            diag(f"reap: migrate issue={num} — recorded {action['state']}")

        elif rule == "re-anchor":
            # The walk PROPOSED this repair; the read-back DECIDES it, over the
            # same REST surface a transition anchors from (§3.1) — so the number
            # written here is comparable to the ones transitions write, and a
            # GraphQL walk that failed to select `totalCount` (floored to 0)
            # cannot talk the reconciler into re-anchoring a whole queue to zero.
            total = api.comment_count(num)
            if total is None:
                return _reconcile_failure("count", num)
            record = states.of(num)
            if record.unknown:
                return _reconcile_failure("state", num)
            if not record.held_state or total >= record.comments:
                # It repaired itself between the walk and here, or the walk was
                # what was wrong. Either way the anchor is already reachable and
                # moving it would be the write this rule exists to avoid.
                diag(f"reap: re-anchor issue={num} — skipped, the record and "
                     "the thread already agree")
                continue
            if not states.write(num, record.re_anchored(total)):
                return _reconcile_failure("state", num)
            # No comment, no label, no ref: the task is as held as it was, and
            # the only thing that changed is that the NEXT reply can be seen.
            diag(f"reap: re-anchor issue={num} — anchor {record.comments} -> "
                 f"{total} ({action['reason']})")

        counts[rule] += 1
    return counts


def project_reconcile(plan: Sequence[ReconcileAction], tasks: list[Task],
                      leases: dict[Issue, Lease],
                      states: dict[Issue, TaskState] | None = None,
                      worker: Worker = RECONCILER_WORKER) -> None:
    """Fold an APPLIED plan back into the in-memory queue read, in place, so the
    drain that just reconciled classifies the reconciled state without paying for
    a second fetch. Mirrors exactly what apply_reconcile wrote — nothing more:
    the leases it deleted, the records it wrote or removed, and the one label
    swap a reclaim performs."""
    by_number = {task.number: task for task in tasks}
    for action in plan:
        rule, num = action["rule"], action["issue"]
        if rule in ("orphan-lock", "reclaim"):
            leases.pop(num, None)
        if states is not None:
            task = by_number.get(num)
            total = task.comment_total if task is not None else 0
            if rule == "orphan-state":
                states.pop(num, None)
            elif rule == "reclaim":
                states[num] = states.get(num, NO_RECORD).moved_to(
                    "needs-decision", worker, total)
            elif rule == "migrate":
                states[num] = TaskState(state=action["state"],
                                        worker=worker,
                                        comments=action["comments"],
                                        recorded=True)
            elif rule == "re-anchor" and num in states:
                # The walk's count, where the applier wrote the read-back's —
                # the same skew `reclaim` above carries, and it cannot matter
                # here: a re-anchor lifts nothing, so this task classifies as
                # held before the repair and after it, whichever count won.
                states[num] = states[num].re_anchored(total)
        task = by_number.get(num)
        if task is None:
            continue
        if rule == "reclaim":
            task.reclaim()


def reconcile_pass(api: Api, worker: Worker, ttl: int | None = None, *,
                   queue: Queue | None = None) -> tuple[int, Json | None]:
    """The reconcile pass as DATA: `(exit_code, counts)`, where counts is None on
    any transport failure. `cmd_reap` is its line-oriented rendering, the same
    split `acquire_next`/`cmd_claim_next` use.

    `queue` defaults to the real one and is injectable so the wiring — which
    failure stages which exit, whether the lease clock reaches the plan — can be
    tested against a scripted queue. The rules themselves are pure and tested
    through `reconcile_plan` directly."""
    got = (queue or Queue(api)).read(ttl=ttl)
    if got is None:
        print("reap: gh-failure stage=list", file=sys.stderr)
        return (EXIT_TRANSPORT, None)
    leases = got.leases

    plan = reconcile_plan(got.tasks, leases, got.states)
    counts = apply_reconcile(api, plan, worker)
    if counts is None:
        return (EXIT_TRANSPORT, None)
    return (EXIT_OK, {"leases": len(leases), **counts})


def cmd_reap(args: argparse.Namespace) -> int:
    """Run the reconcile pass stand-alone — the operator-side escape hatch for
    the reconcile a drain performs on its own (PROTOCOL.md §6). One queue read
    (leases included), then the plan. Exit 0 on success, 20 on any gh/transport
    failure.

    Note what it will NOT do. It does not free an expired lease: expiry is
    applied by whoever READS the queue, so an expired lease is already unheld —
    running `reap` to "unstick" one is a no-op, and the drain that claims next
    steals it. And it does not touch the in-progress label: the label is
    write-only (§3), so a task wearing a stale one is already startable and there
    is nothing to repair."""
    rc, counts = reconcile_pass(args.api, args.worker, args.ttl)
    if counts is None:
        return rc

    print(
        f"reap: done leases={counts['leases']} reclaimed={counts['reclaim']} "
        f"orphan_locks={counts['orphan-lock']} "
        f"orphan_states={counts['orphan-state']} migrated={counts['migrate']} "
        f"re_anchored={counts['re-anchor']}"
    )
    return rc
