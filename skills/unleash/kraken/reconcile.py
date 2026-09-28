"""The §6 repair pass: what to fix, and applying it.

Part of the kraken protocol package; see __init__.py."""
from __future__ import annotations

import argparse
import dataclasses
import sys
from typing import ClassVar, Mapping, Sequence

from .contract import (
    EXIT_OK, EXIT_TRANSPORT, Gen, Issue, Json, Worker, diag
)
from .comments import compose_comment
from .transport import Api, TransportError, stage
from .lease import LEASE_EXPIRY_ESCALATE, Lease
from .refs import Refs
from .state import NO_RECORD, States, TaskState
from .queue import Queue, Task

# --- the reconciler (PROTOCOL.md §6) -----------------------------------------
# A dead worker's lease obstructs only the next claimant, so the READER
# reconciles, on the claim path's queue read — no cron. `reap` runs the same
# pass by hand.

# Who a stand-alone `reap` signs its comments as; a drain uses its own name.
RECONCILER_WORKER = "reconciler"


def stale_claim_body(worker: Worker, reason: str) -> str:
    """The reclaim comment. A worker's token posts it, so it carries the §4
    disclaimer like any worker comment."""
    prose = (
        f"Nobody is finishing this task ({reason}). Stealing the lease again "
        "would just burn another worker, so it needs a human call. To requeue, "
        "reply on this thread — or remove the needs-decision label by hand."
    )
    return compose_comment(
        worker, prose, {"type": "stale-claim", "reason": reason}
    )


# --- the repairs -------------------------------------------------------------
# Each repair is decided by `reconcile_plan`, written by `apply` and folded back
# into the drain's in-memory read by `project`, so the drain classifies what it
# just repaired without a second fetch. Every `apply` is idempotent and ends
# with its ref delete, so a half-applied pass leaves the task held and the next
# reader finishes it (§5's ordering rule).

@dataclasses.dataclass
class Repair:
    issue: Issue
    reason: str
    rule: ClassVar[str] = ""

    def apply(self, api: Api, states: States, worker: Worker) -> bool:
        """Write the repair; False when it turned out to be unnecessary."""
        raise NotImplementedError

    def project(self, leases: dict[Issue, Lease], states: dict[Issue, TaskState],
                task: Task | None, worker: Worker) -> None:
        raise NotImplementedError


@dataclasses.dataclass
class OrphanLock(Repair):
    """Rule 1: a claim ref over a task that already left the claim — closed, no
    longer a task, or holding a state whose ref delete was lost. Delete the ref,
    touch nothing else."""
    gens: list[Gen] = dataclasses.field(default_factory=list)
    rule: ClassVar[str] = "orphan-lock"

    def apply(self, api, states, worker):
        if not Refs(api).drop(self.issue, self.gens):
            raise TransportError("ref")
        diag(f"reap: orphan-lock issue={self.issue} — claim ref deleted")
        return True

    def project(self, leases, states, task, worker):
        leases.pop(self.issue, None)


@dataclasses.dataclass
class OrphanState(Repair):
    """Rule 1's twin: a state record over a task the walk no longer carries.
    Deleting it keeps the namespace from growing one ref per closed task."""
    rule: ClassVar[str] = "orphan-state"

    def apply(self, api, states, worker):
        if not states.delete(self.issue):
            raise TransportError("state")
        diag(f"reap: orphan-state issue={self.issue} — state record deleted")
        return True

    def project(self, leases, states, task, worker):
        states.pop(self.issue, None)


@dataclasses.dataclass
class Reclaim(Repair):
    """Rule 2: a lease that expired `max_expiries` times. A task that kills every
    worker that touches it would otherwise be stolen and dropped forever, so it
    becomes the operator's call: needs-decision, with a stale-claim comment."""
    held: bool = False          # also clear a stale in-progress badge
    gens: list[Gen] = dataclasses.field(default_factory=list)
    rule: ClassVar[str] = "reclaim"

    def apply(self, api, states, worker):
        if not api.post_comment(self.issue,
                                stale_claim_body(worker, self.reason)):
            raise TransportError("comment")
        # The record lands before the ref goes, or the task is observably
        # queued while it waits on a human (§3.1).
        with stage("count"):
            total = api.comment_count(self.issue)
        if not states.write(self.issue, states.of(self.issue).moved_to(
                "needs-decision", worker, total)):
            raise TransportError("state")
        if not api.swap_labels(self.issue,
                               remove="in-progress" if self.held else None,
                               add="needs-decision"):
            raise TransportError("labels")
        if not Refs(api).drop(self.issue, self.gens):
            raise TransportError("ref")
        diag(f"reap: reclaimed issue={self.issue} ({self.reason})")
        return True

    def project(self, leases, states, task, worker):
        leases.pop(self.issue, None)
        total = task.comment_total if task is not None else 0
        states[self.issue] = states.get(self.issue, NO_RECORD).moved_to(
            "needs-decision", worker, total)
        if task is not None:
            task.reclaim()


@dataclasses.dataclass
class Migrate(Repair):
    """Rule 3: a task held by a label with no record — a queue written before
    protocol/9, or a writer that only sets labels. Write the record the label
    implies; post nothing, swap nothing."""
    state: str = ""
    comments: int = 0           # the anchor: any later comment requeues
    rule: ClassVar[str] = "migrate"

    def _record(self, worker: Worker) -> TaskState:
        return TaskState(state=self.state, worker=worker,
                         comments=self.comments, recorded=True)

    def apply(self, api, states, worker):
        if not states.write(self.issue, self._record(worker)):
            raise TransportError("state")
        diag(f"reap: migrate issue={self.issue} — recorded {self.state}")
        return True

    def project(self, leases, states, task, worker):
        states[self.issue] = self._record(worker)


@dataclasses.dataclass
class ReAnchor(Repair):
    """Rule 4: a held record whose anchor outran its thread (comments were
    deleted). §6's derivation compares two integers, so without this a deletion
    buries every reply after it. Move the anchor, keep the hold.

    The walk only PROPOSES it: `apply` re-reads the count over the REST surface
    transitions anchor from, because a walk that failed to select `totalCount`
    floors it to 0 and would re-anchor the whole queue to zero."""
    rule: ClassVar[str] = "re-anchor"

    def apply(self, api, states, worker):
        with stage("count"):
            total = api.comment_count(self.issue)
        record = states.of(self.issue)
        if record.unknown:
            raise TransportError("state")
        if not record.held_state or total >= record.comments:
            diag(f"reap: re-anchor issue={self.issue} — skipped, the record and "
                 "the thread already agree")
            return False
        if not states.write(self.issue, record.re_anchored(total)):
            raise TransportError("state")
        diag(f"reap: re-anchor issue={self.issue} — anchor {record.comments} -> "
             f"{total} ({self.reason})")
        return True

    def project(self, leases, states, task, worker):
        # The walk's count where `apply` wrote the read-back's; harmless, since
        # the task is held either way.
        if self.issue in states:
            total = task.comment_total if task is not None else 0
            states[self.issue] = states[self.issue].re_anchored(total)


REPAIRS = (OrphanLock, OrphanState, Reclaim, Migrate, ReAnchor)


def reconcile_plan(
    tasks: Sequence[Task], leases: dict[Issue, Lease],
    states: Mapping[Issue, TaskState] | None = None,
    max_expiries: int = LEASE_EXPIRY_ESCALATE,
) -> list[Repair]:
    """The §6 repairs one queue read calls for, as a pure function. An expired
    lease is not among them: it is simply not held, and the claim steals it.
    Neither is the in-progress label, which is write-only (§3).

    `tasks` is the repo-wide open-task walk, so a ref on an issue absent from it
    is a ref over a closed task. An empty plan — the common case — costs zero
    writes."""
    states = {} if states is None else states
    by_number = {task.number: task for task in tasks}
    return (_lock_repairs(by_number, leases, states, max_expiries)
            + _orphan_states(by_number, states)
            + _record_repairs(by_number, states))


def _lock_repairs(by_number: Mapping[Issue, Task], leases: dict[Issue, Lease],
                  states: Mapping[Issue, TaskState],
                  max_expiries: int) -> list[Repair]:
    plan: list[Repair] = []
    for num in sorted(leases):
        task = by_number.get(num)
        record = states.get(num, NO_RECORD)
        gens = list(leases[num].gens)
        if task is None or record.holds(task.comment_total):
            plan.append(OrphanLock(num, "the task already left the claim",
                                   gens=gens))
        elif leases[num].expired and record.expiries >= max_expiries:
            plan.append(Reclaim(num, f"the lease expired {record.expiries} "
                                     "times and no worker has finished the task",
                                held="in-progress" in task.labels, gens=gens))
    return plan


def _orphan_states(by_number: Mapping[Issue, Task],
                   states: Mapping[Issue, TaskState]) -> list[Repair]:
    return [OrphanState(num, "the task is no longer an open task")
            for num in sorted(states) if num not in by_number]


def _record_repairs(by_number: Mapping[Issue, Task],
                    states: Mapping[Issue, TaskState]) -> list[Repair]:
    plan: list[Repair] = []
    for num in sorted(by_number):
        task = by_number[num]
        record = states.get(num)
        if record is None:
            if task.held:
                plan.append(Migrate(num, "held by a label with no state record",
                                    state=task.held[0],
                                    comments=task.comment_total))
        elif record.held_state and task.comment_total < record.comments:
            plan.append(ReAnchor(num, f"the record anchors at {record.comments} "
                                      f"comments and the thread carries "
                                      f"{task.comment_total}"))
    return plan


def apply_reconcile(api: Api, plan: Sequence[Repair],
                    worker: Worker) -> dict[str, int]:
    """Execute a plan. Returns per-rule counts; stops at the first transport
    fault, reports it, and re-raises."""
    counts = {repair.rule: 0 for repair in REPAIRS}
    states = States(api)
    for repair in plan:
        try:
            applied = repair.apply(api, states, worker)
        except TransportError as failed:
            print(f"reap: gh-failure stage={failed.stage} issue={repair.issue}",
                  file=sys.stderr)
            raise
        if applied:
            counts[repair.rule] += 1
    return counts


def project_reconcile(plan: Sequence[Repair], tasks: list[Task],
                      leases: dict[Issue, Lease],
                      states: dict[Issue, TaskState] | None = None,
                      worker: Worker = RECONCILER_WORKER) -> None:
    """Fold an APPLIED plan back into the in-memory queue read, in place."""
    states = {} if states is None else states
    by_number = {task.number: task for task in tasks}
    for repair in plan:
        repair.project(leases, states, by_number.get(repair.issue), worker)


def reconcile_pass(api: Api, worker: Worker, ttl: int | None = None, *,
                   queue: Queue | None = None) -> Json:
    """One fresh queue read, reconciled: `{"leases": n, **per-rule counts}`.
    A transport fault is reported and re-raised. `queue` is injectable for
    tests."""
    try:
        got = (queue or Queue(api)).read(ttl=ttl)
    except TransportError:
        print("reap: gh-failure stage=list", file=sys.stderr)
        raise
    plan = reconcile_plan(got.tasks, got.leases, got.states)
    return {"leases": len(got.leases), **apply_reconcile(api, plan, worker)}


def cmd_reap(args: argparse.Namespace) -> int:
    """The reconcile pass by hand (§6). It does not free expired leases — they
    are already unheld — nor touch the write-only in-progress label."""
    try:
        counts = reconcile_pass(args.api, args.worker, args.ttl)
    except TransportError:
        return EXIT_TRANSPORT

    print(
        f"reap: done leases={counts['leases']} reclaimed={counts['reclaim']} "
        f"orphan_locks={counts['orphan-lock']} "
        f"orphan_states={counts['orphan-state']} migrated={counts['migrate']} "
        f"re_anchored={counts['re-anchor']}"
    )
    return EXIT_OK
