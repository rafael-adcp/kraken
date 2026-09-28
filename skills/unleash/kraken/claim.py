"""Taking a task and holding it: the contended claim sequence, the §5 guard,
the drain loop and the heartbeat that renews a lease.

How a worker's turn ENDS is the other half, and it lives in `terminal.py`.
Neither module imports the other — they are siblings over the same refs.

Part of the kraken protocol package; see __init__.py."""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from typing import Any, Callable, Iterable, Union

from .contract import (
    EXIT_LOST, EXIT_NONE, EXIT_NOT_CLEAR, EXIT_OK, EXIT_TRANSPORT,
    EXIT_UNKNOWN_PROJECT, EXIT_USAGE, HELD_LABELS, Issue, Json, Worker, diag
)
from .comments import compose_comment, compose_note, read_body_file
from .transport import Api, TransportError
from .lease import (
    Lease, NO_LEASE, format_age, lease_ttl_seconds, write_claim_state
)
from .refs import Refs
from .state import NO_RECORD, States, TaskState, holding_state
from .queue import PROJECT_CHECK_FAILED, Queue, QueueRead
from .reconcile import apply_reconcile, project_reconcile, reconcile_plan

# --- subcommand: claim -------------------------------------------------------

def lease_expired_body(worker: Worker, stale: Lease) -> str:
    """The thief's audit comment: the only human-readable trace that a task
    changed hands. Posted once per steal."""
    previous = stale.worker or "an unnamed worker"
    age = stale.age
    silence = "its clock could not be read" if age is None \
        else f"it had been silent for {format_age(age)}"
    prose = (
        f"The lease held by `{previous}` expired ({silence}), so this task was "
        f"taken over. If `{previous}` is still alive it will find the lease gone "
        "and stop before writing anything."
    )
    payload = {"type": "lease-expired", "worker": worker,
               "previous_worker": stale.worker or ""}
    if age is not None:
        payload["age_seconds"] = age
    return compose_comment(worker, prose, payload)


def probe_lease_state(api: Api, issue: Issue, ttl: int) -> Lease:
    """One issue's lease, aged on the server's clock (§5.1), for a named claim
    that has no queue read. A stale answer is safe: it decides whether to try,
    and the CAS decides who wins."""
    head = Refs(api).head(issue)
    return head.aged_at(api.server_now(), ttl)


class ClaimAttempt:
    """One worker's attempt to take one task: guard, CAS, projection (§5).
    Each phase answers an exit code to stop with, or None to carry on.

    A drain hands in the `record` and `lease` its queue read already saw; a
    named claim has neither, so it passes `probe` and `ensure_clear` and the
    guard reads them itself — after the cheap label check, so a held task
    still refuses in one read. The lease decides the CAS's starting rung: a
    steal is not a different algorithm, just the generation above an expired
    holder."""

    def __init__(self, api: Api, issue: Issue, worker: Worker, *,
                 record: TaskState = NO_RECORD, lease: Lease = NO_LEASE,
                 ttl: int | None = None, probe: bool = False,
                 ensure_clear: Callable[[], int | None] | None = None):
        self.api = api
        self.refs = Refs(api)
        self.states = States(api)
        self.issue = issue
        self.worker = worker
        self.record = record
        self.lease = lease
        self.ttl = lease_ttl_seconds(ttl)
        self.probe = probe
        self.ensure_clear = ensure_clear or (lambda: None)
        # Decided by the guard, consumed by the projection.
        self.steal = NO_LEASE
        self.stale_held: list[str] = []

    def run(self) -> int:
        """The sequence. Returns an exit code and prints a `claim:` diagnostic."""
        for phase in (self._guard, self._take, self._project):
            code = phase()
            if code is not None:
                return code
        verb = "stole" if self.steal.present else "claimed"
        diag(f"claim: {verb} issue={self.issue} worker={self.worker}")
        return EXIT_OK

    def _guard(self) -> int | None:
        """Refuse a held task with zero writes (§5.2 step 3). One issue fetch
        gives both the live comment count and the no-record label fallback."""
        try:
            detail = self.api.issue_detail(self.issue)
        except TransportError:
            return self._failed("guard")
        total = detail.comment_total
        if total is None:
            return self._failed("guard")
        label_names = detail.labels

        if self.probe:
            self.record = self.states.of(self.issue)
            if self.record.unknown:
                return self._failed("record")
        holding = holding_state(self.record, total, label_names)
        if holding is not None:
            diag(f"claim: held issue={self.issue} label={holding}")
            return EXIT_NOT_CLEAR
        code = self.ensure_clear()
        if code is not None:
            return code
        if self.probe:
            self.lease = probe_lease_state(self.api, self.issue, self.ttl)
            if self.lease.unknown:
                return self._failed("lease")

        # The read must refuse a live lease, because the CAS cannot: creating
        # the generation above it would SUCCEED and take the task from its
        # holder.
        if self.lease.held_by_other(self.worker):
            diag(f"claim: lost-cas issue={self.issue} — another worker holds the claim ref")
            return EXIT_LOST

        self.steal = self.lease if self.lease.stealable_by(self.worker) else NO_LEASE
        # Nothing holds the task, so any held label left on it is stale.
        self.stale_held = [h for h in HELD_LABELS if h in label_names]
        return None

    def _take(self) -> int | None:
        """The CAS: create the generation above whatever was observed."""
        step = self.refs.advance(self.issue, self.lease.gen,
                                 {"type": "claim", "worker": self.worker})
        if step.failed:
            return self._failed(step.stage)
        # A 422 on our own in-flight generation (a retry after a network
        # failure, §5) is not a loss. An unreadable owner counts as not ours.
        if step.lost and self.refs.owner(self.issue) != self.worker:
            diag(f"claim: lost-cas issue={self.issue} — another worker holds the claim ref")
            return EXIT_LOST

        # The generations we climbed past are garbage now.
        if step.won:
            self.refs.drop(self.issue, self.lease.superseded_below(step.gen))
        return None

    def _project(self) -> int | None:
        """Write what a human reads. The local state file goes FIRST so hooks
        can release the claim even if the rest fails; a failure here leaves the
        claim held, and exit 20 says re-check.

        A first claim writes no record (§3.1); a steal writes one to count the
        expiry."""
        write_claim_state(self.api.repo, self.issue, self.worker)
        if self.steal.present:
            if not self.api.post_comment(
                    self.issue, lease_expired_body(self.worker, self.steal)):
                return self._held("comment")
            if not self.states.write(self.issue, self.record.stolen(self.worker)):
                return self._held("record")
        for stale in self.stale_held:
            if not self.api.swap_labels(self.issue, remove=stale):
                return self._held("label")
        if not self.api.swap_labels(self.issue, add="in-progress"):
            return self._held("label")
        body = compose_comment(
            self.worker, "Claimed this task — starting work now.",
            {"type": "claim", "worker": self.worker},
        )
        if not self.api.post_comment(self.issue, body):
            return self._held("comment")
        return None

    def _failed(self, stage: str) -> int:
        """A read or the CAS did not land: nothing is ours yet, so re-check."""
        diag(f"claim: gh-failure issue={self.issue} stage={stage}")
        return EXIT_TRANSPORT

    def _held(self, stage: str) -> int:
        """A projection write that did not land after the CAS won: the claim is
        ours, so exit 20 means re-check, not re-claim."""
        diag(f"claim: gh-failure issue={self.issue} stage={stage} (claim held)")
        return EXIT_TRANSPORT


def _claim_once(api: Api, issue: Issue, worker: Worker,
                record: TaskState = NO_RECORD, lease: Lease = NO_LEASE,
                ttl: int | None = None, probe: bool = False,
                ensure_clear: Callable[[], int | None] | None = None) -> int:
    """The claim sequence as a call, injectable as a drain's `claim_step`."""
    return ClaimAttempt(api, issue, worker, record=record, lease=lease,
                        ttl=ttl, probe=probe, ensure_clear=ensure_clear).run()


def refuse_second_claim(read: QueueRead, worker: Worker,
                        issue: Issue | str | None = None) -> Issue | None:
    """§5's one-task-at-a-time rule, from a queue read: the issue this worker
    already holds, or None when it is clear. Derived from the ladder, so a lost
    local scratch file can neither brick a worker nor free it.

    Presence decides, not the TTL: an expired lease of ours may still finish its
    transition (§5.3). A ref on a task that left the queue is moot. A ref on the
    same `issue` is a permitted re-claim after a network failure."""
    tasks = read.by_number()
    for held, lease in sorted(read.leases.items()):
        if not lease.held_by(worker):
            continue
        task = tasks.get(held)
        if task is None or claim_is_moot(read.record(held), task.comment_total,
                                         task.labels):
            continue
        if issue is not None and str(held) == str(issue):
            continue
        diag(refused_line(worker, held))
        return held
    return None


def claim_is_moot(record: TaskState, comment_total: int,
                  labels: Iterable[str] = ()) -> bool:
    """Whether a claim ref of ours is moot: its task already reached a held
    state, so this worker's turn on it is over. (A task that left the open walk
    is moot too; the caller decides that one.)"""
    return holding_state(record, comment_total, labels) is not None


def refused_line(worker: Worker, held: Issue) -> str:
    """The §5 refusal, worded once — both guards print the identical sentence."""
    return (f"claim: refused worker={worker} holds={held} — one task at a time "
            f"(PROTOCOL.md §5); resolve the open claim first "
            f"(deliver / escalate / release)")


def open_claim_of(api: Api, worker: Worker, issue: Issue | str | None = None,
                  ttl: int | None = None) -> Issue | None:
    """`refuse_second_claim` for a caller with no queue read, decided from the
    ladder alone: the issue this worker already holds, or None when it is
    clear. Two batched calls plus one issue fetch per claim found."""
    leases, _commit_meta, state_shas = Queue(api).lease_view(ttl=ttl)

    for held, lease in sorted(leases.items()):
        if not lease.held_by(worker):
            continue
        # A re-claim of the same issue is permitted, and needs no fetch.
        if issue is not None and str(held) == str(issue):
            continue
        detail = api.issue_detail(held)
        if not detail.open or "kraken-task" not in detail.labels:
            continue                    # left the queue: moot, and no record to read
        record = States(api).at(state_shas[held]) if held in state_shas else NO_RECORD
        if record.unknown:
            raise TransportError()
        if claim_is_moot(record, detail.comment_total, detail.labels):
            continue
        diag(refused_line(worker, held))
        return held
    return None


def cmd_claim(args: argparse.Namespace) -> int:
    def ensure_clear() -> int | None:
        try:
            held = open_claim_of(args.api, args.worker, args.issue)
        except TransportError:
            diag("claim: gh-failure stage=lease")
            return EXIT_TRANSPORT
        return None if held is None else EXIT_NOT_CLEAR

    return _claim_once(args.api, args.issue, args.worker, probe=True,
                       ensure_clear=ensure_clear)


# --- subcommand: claim-next --------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Claimed:
    """A won claim, with what the state record already knew about the task's
    past: `bounced` is §6's requeue derivation, `pr` is where the last delivery
    went (§8), and `anchor` is the comment index the new feedback starts at."""
    issue: Issue
    title: str
    body: str
    bounced: bool
    pr: str | None
    anchor: int
    exit_code = EXIT_OK

    def as_json(self) -> Json:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class AlreadyHolding:
    """§5 refused a second task: this worker already holds `issue`."""
    issue: Issue
    exit_code = EXIT_NOT_CLEAR


@dataclasses.dataclass(frozen=True)
class NoClaim:
    """Nothing was won; the exit code says why."""
    exit_code: int


Acquisition = Union[Claimed, AlreadyHolding, NoClaim]


def cmd_claim_next(args: argparse.Namespace) -> int:
    """`acquire_next`, rendered for a human or a shell."""
    got = acquire_next(args.api, args.project, args.worker)
    if isinstance(got, Claimed):
        if args.json:
            print(json.dumps(got.as_json()))
        else:
            diag(f"claim-next: claimed issue={got.issue} worker={args.worker}")
            print(f"{got.issue}\t{got.title}")
            print()
            print(got.body)
    return got.exit_code


def acquire_next(api: Api, project: str, worker: Worker,
                 ttl: int | None = None, *,
                 queue: Queue | None = None,
                 claim_step: Callable[..., Any] | None = None) -> Acquisition:
    """The deterministic claim loop. `claim-next` and `next-action` are two
    renderings of this one result, so they cannot drift."""
    return Drain(api, project, worker, ttl,
                 queue=queue, claim_step=claim_step).run()


class Drain:
    """One pass of the claim loop: project preflight, queue read, §5 guard, §6
    reconcile, then guard + CAS down the candidates until one wins. A lost or
    held candidate moves forward, never back; a transport fault stops.

    `queue` and `claim_step` are injectable so the iteration can be tested
    against scripted outcomes; production passes neither."""

    def __init__(self, api: Api, project: str, worker: Worker,
                 ttl: int | None = None, *, queue: Queue | None = None,
                 claim_step: Callable[..., Any] | None = None):
        self.api = api
        self.project = project
        self.worker = worker
        self.ttl = lease_ttl_seconds(ttl)
        self.queue = queue or Queue(api)
        self.claim_step = claim_step or _claim_once
        self.read: QueueRead | None = None

    def run(self) -> Acquisition:
        for phase in (self._preflight, self._read, self._guard,
                      self._reconcile):
            stopped = phase()
            if stopped is not None:
                return stopped
        return self._claim_first_startable()

    def _preflight(self) -> Acquisition | None:
        # Before any read or write: a worker scoped to a label the repo does not
        # carry is deaf, not idle — it would report an empty queue forever.
        try:
            check = self.queue.check_project(self.project)
        except TransportError:
            return self._transport(PROJECT_CHECK_FAILED)
        if check.carried:
            return None
        diag(check.refusal)
        return NoClaim(EXIT_UNKNOWN_PROJECT)

    def _read(self) -> Acquisition | None:
        try:
            self.read = self.queue.read(ttl=self.ttl)
        except TransportError:
            return self._transport("claim-next: gh-failure stage=list")
        return None

    def _guard(self) -> Acquisition | None:
        # Decided before the reconcile, so a refusal writes nothing.
        held = refuse_second_claim(self.read, self.worker)
        return None if held is None else AlreadyHolding(held)

    def _reconcile(self) -> Acquisition | None:
        # The next claimant is the only party a dead worker's lease obstructs,
        # so the repair rides here, on the read already paid for.
        read = self.read
        plan = reconcile_plan(read.tasks, read.leases, read.states)
        if not plan:
            return None
        try:
            apply_reconcile(self.api, plan, self.worker)
        except TransportError:
            return self._transport(
                "claim-next: gh-failure stage=reconcile — state unknown, re-check")
        project_reconcile(plan, read.tasks, read.leases, read.states)
        return None

    def _claim_first_startable(self) -> Acquisition:
        try:
            rows = self.queue.candidates(self.project, read=self.read)
        except TransportError:
            return self._transport("claim-next: gh-failure stage=list")
        for cand in rows:  # priority-first, then FIFO
            if not cand.startable:
                continue
            record = self.read.record(cand.number)
            rc = self.claim_step(
                self.api, cand.number, self.worker, record=record,
                lease=self.read.leases.get(cand.number, NO_LEASE), ttl=self.ttl)
            if rc == EXIT_OK:
                # A first claim writes no record (§3.1), so the one read here
                # is still current.
                return Claimed(cand.number, cand.title, cand.body,
                               bounced=record.requeued(cand.task.comment_total),
                               pr=record.pr, anchor=record.comments)
            if rc == EXIT_TRANSPORT:
                # A write of ours may have half-landed: never move on to
                # another candidate.
                return self._transport(f"claim-next: gh-failure issue={cand.number}"
                                       " — state unknown, re-check")
            # EXIT_LOST / EXIT_NOT_CLEAR: try the next candidate.
        diag(f"claim-next: none project:{self.project}")
        return NoClaim(EXIT_NONE)

    @staticmethod
    def _transport(line: str) -> NoClaim:
        diag(line)
        return NoClaim(EXIT_TRANSPORT)


# --- subcommand: heartbeat ---------------------------------------------------

def cmd_heartbeat(args: argparse.Namespace) -> int:
    """Renew the lease by climbing one generation. It is a CAS, not an update,
    so a renewal and a steal race on the same ref and the loser is told."""
    api, issue, worker, message = args.api, args.issue, args.worker, args.message
    refs = Refs(api)
    hold = refs.hold(issue, worker)
    if hold.refused:
        diag(f"heartbeat: {hold.reason}")
        return hold.code
    step = refs.advance(issue, hold.head.gen,
                        {"type": "heartbeat", "worker": worker, "msg": message})
    if step.failed:
        diag(f"heartbeat: gh-failure issue={issue} stage={step.stage}")
        return EXIT_TRANSPORT
    if step.lost:
        diag(f"heartbeat: lost-lease issue={issue} — another worker took the "
             "lease while this one was silent")
        return EXIT_LOST
    refs.drop(issue, hold.head.superseded_below(step.gen))
    diag(f"heartbeat: renewed issue={issue} worker={worker}")
    return EXIT_OK


# --- subcommand: note --------------------------------------------------------

def cmd_note(args: argparse.Namespace) -> int:
    """Post a free-form worker comment that changes no state (§4)."""
    api, issue, worker, body_file = args.api, args.issue, args.worker, args.body_file
    if not os.path.isfile(body_file):
        print(f"note: no such file {body_file}", file=sys.stderr)
        return EXIT_USAGE

    prose = read_body_file(body_file)
    if not prose.strip():
        print(f"note: empty body {body_file}", file=sys.stderr)
        return EXIT_USAGE

    body = compose_note(worker, prose)
    if not api.post_comment(issue, body):
        print(f"note: gh-failure issue={issue} stage=comment")
        return EXIT_TRANSPORT

    print(f"note: posted issue={issue} worker={worker}")
    return EXIT_OK
