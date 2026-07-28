"""The §6 repair pass: what to fix, and applying it.

Part of the kraken protocol package; see __init__.py."""
from __future__ import annotations

import argparse
import sys
from typing import Any, Callable, Sequence

from .contract import (
    EXIT_OK, EXIT_TRANSPORT, Issue, Json, ReconcileAction, Worker, diag
)
from .comments import compose_comment
from .transport import Api
from .lease import LEASE_EXPIRY_ESCALATE, Lease
from .refs import Refs
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
    max_expiries: int = LEASE_EXPIRY_ESCALATE,
) -> list[ReconcileAction]:
    """The reconciler's decision as a PURE function of one queue read — no
    network, so every rule is unit-testable in isolation (PROTOCOL.md §6). The
    expiry itself is already decided (`lease_state`); these are the repairs the
    expiry does NOT make on its own:

      1. **orphan lock** — a ref whose issue is not an open task any more, or is
         already labeled needs-decision/awaiting-merge (a terminal transition
         whose ref delete was lost): delete the ref, touch nothing else;
      2. **reclaim** — a task whose lease has expired `max_expiries` times
         already: move it to needs-decision with a stale-claim comment, then
         delete the ref. This is the ONLY thing an expiry escalates. A task that
         kills whatever worker touches it — a poisoned environment, a step that
         always hangs — would otherwise be stolen, dropped, stolen again for as
         long as the queue has workers, each round burning a drain and telling
         nobody. Anything below the threshold is not the reconciler's business:
         the lease is simply not held, and the claim path steals it.

    Both rules are about the LEASE, and none is about the in-progress LABEL: that
    label is write-only (§3), so a wrong badge misleads nobody and misroutes
    nothing — it is not a repair the reader owes anyone, and the next transition
    on the task overwrites it anyway.

    `tasks` is the open-kraken-task walk (repo-wide, before any project filter),
    so rule 1 needs no per-ref issue read: a ref on an issue absent from that
    walk is a ref on an issue that is closed, or no longer a task at all —
    either way it holds a lock over nothing. Nothing on the issue timeline
    anchors liveness, so an operator poking a dead worker's thread shortens
    time-to-triage rather than extending the lease.

    Returns a list of `{"rule", "issue", "reason"}` actions ordered by rule then
    issue number; an empty list means every lease is one a worker is entitled to
    hold (the overwhelmingly common case, and it costs zero writes)."""
    by_number = {task.number: task for task in tasks}
    plan = []

    # Every rule is keyed on a lease, so the walk is over the refs — a task with
    # no ref has nothing for this pass to repair, whatever labels it wears.
    for num in sorted(leases):
        task = by_number.get(num)
        if task is None or task.held:
            plan.append({"rule": "orphan-lock", "issue": num,
                         "reason": "the task already left the claim",
                         "gens": list(leases[num].gens)})
            continue

        if leases[num].expired:
            expiries = task.expiries
            if expiries >= max_expiries:
                plan.append({"rule": "reclaim", "issue": num,
                             # The escalation writes needs-decision, and clears
                             # the in-progress badge if the task still wears one:
                             # write-only does not mean write-and-forget.
                             "reason": f"the lease expired {expiries} times and "
                                       "no worker has finished the task",
                             "held": "in-progress" in task.labels,
                             "gens": list(leases[num].gens)})
            # Below the threshold: not held, not repaired — stolen by the claim.

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
    counts = {"orphan-lock": 0, "reclaim": 0}
    for action in plan:
        rule, num = action["rule"], action["issue"]

        if rule == "orphan-lock":
            if not Refs(api).drop(num, action["gens"]):
                return _reconcile_failure("ref", num)
            diag(f"reap: orphan-lock issue={num} — claim ref deleted")

        elif rule == "reclaim":
            if not api.swap_labels(
                num,
                remove="in-progress" if action["held"] else None,
                add="needs-decision",
            ):
                return _reconcile_failure("labels", num)
            if not api.post_comment(
                    num, stale_claim_body(worker, action["reason"])):
                return _reconcile_failure("comment", num)
            if not Refs(api).drop(num, action["gens"]):
                return _reconcile_failure("ref", num)
            diag(f"reap: reclaimed issue={num} ({action['reason']})")

        counts[rule] += 1
    return counts


def project_reconcile(plan: Sequence[ReconcileAction], tasks: list[Task],
                      leases: dict[Issue, Lease]) -> None:
    """Fold an APPLIED plan back into the in-memory queue read, in place, so the
    drain that just reconciled classifies the reconciled state without paying for
    a second fetch. Mirrors exactly what apply_reconcile wrote — nothing more:
    the leases it deleted, and the one label swap a reclaim performs."""
    by_number = {task.number: task for task in tasks}
    for action in plan:
        rule, num = action["rule"], action["issue"]
        if rule in ("orphan-lock", "reclaim"):
            leases.pop(num, None)
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

    plan = reconcile_plan(got.tasks, leases)
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
        f"orphan_locks={counts['orphan-lock']}"
    )
    return rc
