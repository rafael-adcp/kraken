"""Taking a task and holding it: the contended claim sequence, the §5 guard,
the drain loop and the heartbeat that renews a lease.

How a worker's turn ENDS is the other half, and it lives in `terminal.py`.
Neither module imports the other — they are siblings over the same refs.

Part of the kraken protocol package; see __init__.py."""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Callable, Iterable

from .contract import (
    EXIT_LOST, EXIT_NONE, EXIT_NOT_CLEAR, EXIT_OK, EXIT_TRANSPORT,
    EXIT_UNKNOWN_PROJECT, EXIT_USAGE, HELD_LABELS, Issue, Json, Worker, diag
)
from .comments import compose_comment, compose_note, read_body_file
from .transport import Api, comment_total_of
from .lease import (
    Lease, NO_LEASE, format_age, lease_ttl_seconds, write_claim_state
)
from .refs import Refs
from .state import NO_RECORD, States, TaskState, holding_state
from .queue import Queue, QueueRead
from .reconcile import apply_reconcile, project_reconcile, reconcile_plan

# --- subcommand: claim -------------------------------------------------------

def lease_expired_body(worker: Worker, stale: Lease) -> str:
    """The thief's audit comment: who held the lease, how long it had been
    silent, and who took it. Two jobs — it is the only human-readable trace that
    a task changed hands, and its `lease-expired` marker is what §6's repeat
    guard counts, so one comment per steal, never one per read."""
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
    """Read one issue's lease into the same `Lease` a queue read produces —
    `NO_LEASE` when the task carries no claim ref at all, `UNREADABLE_LEASE` on
    a transport failure, so 'unclaimed' is never confused with 'unknown'.

    A drain never needs this: the queue read already carries every lease. It is
    the named-claim path's equivalent, and a slightly stale answer is safe by
    construction — the CAS that follows competes by CREATING the generation above
    the one this saw, so a holder that renewed in the meantime already took that
    generation and the challenger simply loses. This read decides whether to
    *try*, never who wins.

    The clock is the SERVER's (§5.1), asked for after the ref read so it is
    anchored to the same machine that stamped the commit date it ages."""
    head = Refs(api).head(issue)
    return head.aged_at(api.server_now(), ttl)


class ClaimAttempt:
    """One worker's attempt to take one task: guard, CAS, projection, executed
    identically every time (PROTOCOL.md §5). Shared by `claim` and `claim-next`
    so the two can never drift.

    The constructor arguments below are not really parameters of a call — they
    are what an attempt IS, which is why they live on the object rather than
    threading through the phases. Each phase is a method that answers an exit
    code to stop with, or None to carry on; `run` sequences them and owns the one
    success path.

    Constructor arguments:

    `record` is the state record the caller already read for this issue (§3.1),
    or None when it has not read one — a drain gets every record with its queue
    read, a `claim <issue>` gets none, so the guard fetches it. Handing it in is
    what stops the guard from refusing the very task the queue filter just
    offered: both derive "is it held" from the same record and the same count.
    The guard is an optimisation either way — the claim-ref CAS is the only thing
    that decides ownership.

    `lease` is the lease record the caller observed on this issue, or `NO_LEASE`
    when it observed no claim ref. It decides the generation the CAS starts
    from — a free task takes generation 1, and a task whose lease expired takes
    the one ABOVE its holder. That is the whole of what a "steal" is: not a
    different algorithm, a different starting rung.

    `probe_lease` is the named-claim path's substitute for that observation: a
    drain reads every lease with the queue, a `claim <issue>` has read nothing,
    so it looks the lease up itself — but only once the cheap label guard has
    already let the task through, so a task held by `needs-decision` or
    `awaiting-merge` still costs exactly one read to refuse.

    `ensure_clear` is the caller's §5 one-task-at-a-time observation, on the
    same principle: `claim-next` derived it from the queue read it already
    holds, so it passes nothing; a named claim reads a queue of its own through
    this callback — sequenced after the label guard for the same reason the
    probe is, so a held task refuses without a queue read."""

    def __init__(self, api: Api, issue: Issue, worker: Worker, *,
                 record: TaskState | None = None, lease: Lease = NO_LEASE,
                 ttl: int | None = None, probe_lease: bool = False,
                 ensure_clear: Callable[[], int | None] | None = None):
        self.api = api
        self.refs = Refs(api)
        self.states = States(api)
        self.issue = issue
        self.worker = worker
        self.record = record
        self.lease = lease
        self.ttl = lease_ttl_seconds(ttl)
        self.probe_lease = probe_lease
        self.ensure_clear = ensure_clear or (lambda: None)
        # Decided by the guard, consumed by the projection: whose expired lease
        # this is taking, and which stale held labels are still on the issue.
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
        """Refuse early and for free: a held task is skipped with zero writes.

        The verdict is the STATE RECORD, re-derived over a fresh read (§5.2 step
        3): the record names the state and its `comments` anchor says whether the
        thread has moved past it, so a task an operator has commented on is
        claimed rather than refused. One issue fetch answers both halves — the
        labels for the no-record fallback (§3.1) and the live comment count — so
        the cheap refusal stays one request.

        `in-progress` is not among the labels consulted: it is write-only (§3),
        and the lease below is what decides whether somebody is working."""
        obj = self.api.issue_detail(self.issue)
        total = None if obj is None else comment_total_of(obj)
        if obj is None or total is None:
            diag(f"claim: gh-failure issue={self.issue} stage=guard")
            return EXIT_TRANSPORT
        label_names = [lbl.get("name", "") for lbl in obj.get("labels", [])]

        if self.record is None:
            self.record = self.states.of(self.issue)
            if self.record.unknown:
                diag(f"claim: gh-failure issue={self.issue} stage=record")
                return EXIT_TRANSPORT
        holding = holding_state(self.record, total, label_names)
        if holding is not None:
            diag(f"claim: held issue={self.issue} label={holding}")
            return EXIT_NOT_CLEAR
        code = self.ensure_clear()
        if code is not None:
            return code
        if self.probe_lease and not self.lease.present:
            # Labels say nothing about who is working — that is the lease's job,
            # and this path has not read one yet. It answers both questions at
            # once: who holds the task (the check below), and which generation
            # the CAS must create — the one above whatever is actually there,
            # since creating a LOWER one would "win" a ref nobody is holding by
            # while the real holder works on.
            self.lease = probe_lease_state(self.api, self.issue, self.ttl)
            if self.lease.unknown:
                diag(f"claim: gh-failure issue={self.issue} stage=lease")
                return EXIT_TRANSPORT

        # A live lease that is not ours holds the task whatever the labels say —
        # the lock is the whole of "somebody is working on this" (§5), including
        # in the crash window where no label has landed yet, and including when
        # the badge says otherwise. This is a LOST claim, not an unclear one:
        # somebody owns the task. The READ has to answer it, because the CAS
        # cannot: creating the generation above a live lease would SUCCEED and
        # take the task out from under its holder, so there is no 422 to read.
        if self.lease.held_by_other(self.worker):
            diag(f"claim: lost-cas issue={self.issue} — another worker holds the claim ref")
            return EXIT_LOST

        self.steal = self.lease if self.lease.stealable_by(self.worker) else NO_LEASE
        # Any held label still on the issue is stale by now — the guard above
        # just established that nothing holds this task — so the projection swaps
        # every one of them off for `in-progress`.
        self.stale_held = [h for h in HELD_LABELS if h in label_names]
        return None

    def _take(self) -> int | None:
        """The CAS — an orphan claim commit, then the create that arbitrates: the
        generation above whatever was observed. It is the SAME conflict-failing
        write on the SAME ref name for a first claim, a steal and a renewal, so N
        thieves racing one expired lease — and the holder racing them with a
        renewal — produce exactly one winner and nothing to undo. Nothing is
        deleted to make room, so the task is never observably free mid-steal and
        no interleaving can hand two workers the same lease."""
        outcome, gen, _sha = self.refs.advance(
            self.issue, self.lease.gen,
            {"type": "claim", "worker": self.worker})
        if outcome.startswith("fail"):
            diag(f"claim: gh-failure issue={self.issue} stage={outcome[len('fail-'):]}")
            return EXIT_TRANSPORT
        if outcome == "lost":
            # A 422 is a real loss only if the lease is ANOTHER worker's. A
            # worker re-claiming its own in-flight generation after a network
            # failure (§5) already owns the task, so fall through to re-project.
            # An unreadable owner counts as not ours.
            if self.refs.owner(self.issue) != self.worker:
                diag(f"claim: lost-cas issue={self.issue} — another worker holds the claim ref")
                return EXIT_LOST

        # The generations we climbed past are garbage now — the holder is the
        # highest one, so a delete that fails leaves a stray ref, never a second
        # lease.
        if gen is not None:
            self.refs.drop(self.issue,
                             self.lease.superseded_below(gen))
        return None

    def _project(self) -> int | None:
        """Write what a human reads. State file FIRST so lifecycle hooks can
        release the claim even if the writes below fail; then — on a steal — the
        `lease-expired` comment and the expiry count that goes with it; then the
        in-progress label and claim comment. A failure here leaves the claim HELD
        by the lease — exit 20 says re-check.

        A first claim writes NO record (§3.1): it changes nothing about what the
        task is between workers, and the lease is what says it is being executed.
        A steal writes one because the count it carries is cumulative and §6's
        repeat-expiry guard reads it — an increment that does not land costs one
        further steal before that guard fires, never a wrong state."""
        write_claim_state(self.api.repo, self.issue, self.worker)
        if self.steal.present:
            if not self.api.post_comment(
                    self.issue, lease_expired_body(self.worker, self.steal)):
                return self._held("comment")
            # The guard always leaves a record behind (read or handed in), so
            # there is nothing to fetch here — and nothing to guess: a task that
            # never had one starts its expiry count at this steal.
            record = self.record if self.record is not None else NO_RECORD
            if not self.states.write(self.issue, record.stolen(self.worker)):
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

    def _held(self, stage: str) -> int:
        """A projection write that did not land. The CAS already won, so the
        claim IS ours — the diagnostic says so, and exit 20 means re-check
        rather than re-claim."""
        diag(f"claim: gh-failure issue={self.issue} stage={stage} (claim held)")
        return EXIT_TRANSPORT


def _claim_once(api: Api, issue: Issue, worker: Worker,
                record: TaskState | None = None, lease: Lease = NO_LEASE,
                ttl: int | None = None, probe_lease: bool = False,
                ensure_clear: Callable[[], int | None] | None = None) -> int:
    """The claim sequence as a call. Kept because `acquire_next` injects it as
    `claim_step` and both subcommands reach it through here."""
    return ClaimAttempt(api, issue, worker, record=record, lease=lease,
                        ttl=ttl, probe_lease=probe_lease,
                        ensure_clear=ensure_clear).run()


def refuse_second_claim(read: QueueRead, worker: Worker,
                        issue: Issue | str | None = None) -> Issue | None:
    """PROTOCOL.md §5: a worker MUST work one task at a time and MUST NOT claim
    a second task while it holds a lease. Derived from the queue read itself —
    the ladder is the truth, so a lost or stale claim-<worker>.json can neither
    brick this worker nor let it take a second task. Returns the held issue when
    refusing (the caller answers EXIT_NOT_CLEAR, writing nothing), None when the
    worker is clear.

    Presence decides, never the TTL: an expired-but-ours lease may still finish
    its transition (§5.3), so it is an unresolved claim, not a free worker. A
    ref on a task that already left the queue — closed (absent from the open
    walk) or held by a terminal label — is a moot ref awaiting collection, not
    an open claim, exactly the cases `resume_verdict` calls resolved.

    A held ref on the *same* `issue` is a permitted re-claim, not a second task:
    it is exactly the §5 network-failure caveat ("or while a claim of its own is
    in an unknown state after a network failure — re-check first"). `issue=None`
    (claim-next, always taking a *new* task) refuses on any open claim."""
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
    """Whether a claim ref of ours is a moot ref awaiting collection rather than
    an open claim: the task it locks has already reached a held state, so this
    worker's turn on it is over — exactly the cases `resume_verdict` calls
    resolved. A ref whose task left the OPEN walk entirely is moot too, and the
    caller decides that one: absence from the walk is not a state this can read.

    Decided by `holding_state`, which is the §3.1 read rule itself — where a
    record exists it decides and the labels are not consulted, and only a task
    with no record falls back to its badge. Consulting the label unconditionally
    would disagree with §6's rule 1 (`reconcile_plan`): a delivered task the
    operator requeued by commenting reads as still-held by its stale badge and
    as free by its record, and two answers to "did the task leave my claim" is
    one too many.

    One predicate, because the same rule is decided from three observations: the
    drain reads it off the queue walk it already has, a named claim off one issue
    fetch plus one record (`open_claim_of`), and the resume path off the same
    pair (`next_action.issue_is_finished`)."""
    return holding_state(record, comment_total, labels) is not None


def refused_line(worker: Worker, held: Issue) -> str:
    """The §5 refusal, worded once — both guards print the identical sentence."""
    return (f"claim: refused worker={worker} holds={held} — one task at a time "
            f"(PROTOCOL.md §5); resolve the open claim first "
            f"(deliver / escalate / release)")


def open_claim_of(api: Api, worker: Worker, issue: Issue | str | None = None,
                  ttl: int | None = None) -> tuple[bool, Issue | None]:
    """The §5 one-task-at-a-time guard for a caller that holds no queue read —
    the same rule and the same verdict as `refuse_second_claim`, decided from the
    claim-ref LADDER instead of the issue walk. Returns `(ok, held)`; `ok` is
    False only on transport failure, and only then, so "this worker is clear" is
    never confused with "the read did not land".

    The ladder is what arbitrates ownership (§5), so it is also what can answer
    this — and answering it from the queue instead was costing a named `claim` a
    paginated issue walk plus a comment hydration to learn something no issue
    body was ever going to say. Here it is two batched calls, plus one issue
    fetch per claim actually found, which in practice is zero or one.

    The alternative considered and rejected was a server-side per-worker index
    ref. It could only ever be best-effort — a failed index write must not fail a
    won claim — so a missing entry would be indistinguishable from a clear
    worker, which turns a MUST into a hint. The ladder cannot lie about this."""
    got = Queue(api).lease_view(ttl=ttl)
    if got is None:
        return (False, None)
    leases, _commit_meta, state_shas = got

    for held, lease in sorted(leases.items()):
        if not lease.held_by(worker):
            continue
        # A re-claim of the SAME issue is permitted (§5's network-failure
        # caveat), and deciding it needs no fetch — so it is decided first.
        if issue is not None and str(held) == str(issue):
            continue
        obj = api.issue_detail(held)
        if obj is None:
            return (False, None)
        labels = {lbl.get("name", "") for lbl in obj.get("labels", [])}
        if str(obj.get("state", "")).upper() != "OPEN" or "kraken-task" not in labels:
            continue                    # left the queue: moot, and no record to read
        # The record, resolved from the sha the ref read above already carried —
        # one commit read, and only for a task this worker actually holds. A
        # worker that is clear pays nothing for it.
        record = States(api).at(state_shas[held]) if held in state_shas else NO_RECORD
        if record.unknown:
            return (False, None)
        if claim_is_moot(record, comment_total_of(obj), labels):
            continue
        diag(refused_line(worker, held))
        return (True, held)
    return (True, None)


def cmd_claim(args: argparse.Namespace) -> int:
    def ensure_clear() -> int | None:
        # The §5 guard, derived from the claim refs rather than the local scratch
        # file — and from the LADDER alone, not a whole queue read: a named claim
        # is asking who holds what, which is the one question the refs answer by
        # themselves. The attempt calls this only once the free label guard has
        # let the task through, so a held task still refuses without touching the
        # git-data API at all.
        ok, held = open_claim_of(args.api, args.worker, args.issue)
        if not ok:
            diag("claim: gh-failure stage=lease")
            return EXIT_TRANSPORT
        return EXIT_NOT_CLEAR if held is not None else None

    # The target's lease is probed by the attempt itself: an expired one does
    # not hold the task, it gets stolen (§5).
    return _claim_once(args.api, args.issue, args.worker, probe_lease=True,
                       ensure_clear=ensure_clear)


# --- subcommand: claim-next --------------------------------------------------

def cmd_claim_next(args: argparse.Namespace) -> int:
    """Collapse the deterministic claim loop into one invocation: read the queue,
    reconcile it (§6), then list startable candidates oldest-first and guard + CAS
    each in turn, stopping at the first win. Losses (10/11) move to the next
    candidate; a transport fault (20) stops with state-unknown; an exhausted queue
    is EXIT_NONE. Never retries a lost CAS on the same issue (PROTOCOL.md §5) — it
    iterates forward, never back.

    The loop itself lives in `acquire_next`; this is its line-oriented rendering
    (the briefing a human or a shell reads). `next-action` renders the same
    result as a JSON envelope."""
    rc, won = acquire_next(args.api, args.project, args.worker)
    if rc == EXIT_OK:
        if args.json:
            print(json.dumps(won))
        else:
            diag(f"claim-next: claimed issue={won['issue']} worker={args.worker}")
            print(f"{won['issue']}\t{won['title']}")
            print()
            print(won["body"])
    return rc


def acquire_next(api: Api, project: str, worker: Worker,
                 ttl: int | None = None, *,
                 queue: Queue | None = None,
                 claim_step: Callable[..., Any] | None = None,
                 ) -> tuple[int, Json | None]:
    """The whole acquisition — project preflight, queue read, the §5
    one-task-at-a-time guard, §6 reconcile, then guard + CAS down the candidate
    list — as DATA rather
    than as printed output: returns `(exit_code, won)` where `won` is
    `{"issue", "title", "body", "bounced", "pr", "anchor"}` on EXIT_OK,
    `{"issue"}` naming the already-held claim on EXIT_NOT_CLEAR, and None on
    every other outcome.

    `bounced`, `pr` and `anchor` come off the state record this read already
    carried, and they are here because the record is READ HERE and nowhere else
    afterwards: a first claim writes none (§3.1), so a caller that wanted any of
    them would have to fetch what this function had in hand and threw away.
    `bounced` is §6's requeue derivation — two integers, exact — so no reader
    downstream has to re-derive "is this task coming back" from the thread, and
    `pr` is where the last delivery went, so rework continues on that branch
    instead of opening a second PR beside it.

    `anchor` is the OTHER half of that derivation: the comment total frozen into
    the record, which is the index the new comments start at. `bounced` says the
    thread moved on; `anchor` says WHERE, and a reader holding both can cut the
    feedback out of the thread instead of judging by eye which comments are new.
    Carried as the integer rather than as the comments themselves because this
    function is the deterministic claim loop and reading a thread is not part of
    it — `next-action` pays for that read, and only when `bounced` is true.

    `claim-next` and `next-action` are both thin renderings of this, which is
    what keeps the deterministic claim loop from drifting between them: there is
    one implementation and two presentations. Diagnostics go through `diag`, so
    `next-action` can push them to stderr and keep stdout pure JSON.

    `queue` and `claim_step` default to the real thing and exist so the
    ITERATION — skip-on-held, skip-on-lost, forward-only, stop-on-transport — can
    be tested against a scripted queue and scripted claim outcomes. Reading the
    queue and filtering it are one collaborator, which is what `Queue` is for.
    Both have their own tests; a caller in production passes neither."""
    queue = queue or Queue(api)
    claim_step = claim_step or _claim_once

    # Project preflight: before the queue is read and before any write,
    # because a worker scoped to a project label the repo does not carry is deaf,
    # not idle — it would report an honest-looking empty queue forever.
    ok, message = queue.verify_project(project)
    if ok is None:
        diag(message)
        return (EXIT_TRANSPORT, None)
    if not ok:
        diag(message)
        return (EXIT_UNKNOWN_PROJECT, None)

    ttl = lease_ttl_seconds(ttl)
    read = queue.read(ttl=ttl)
    if read is None:
        diag("claim-next: gh-failure stage=list")
        return (EXIT_TRANSPORT, None)
    leases = read.leases

    # One task at a time (§5), decided by the read that just landed — before the
    # reconcile below can touch anything, so a refusal writes nothing. The held
    # issue rides back with the exit code so `next-action` can resume the claim
    # this worker provably holds instead of reporting a dead end.
    held = refuse_second_claim(read, worker)
    if held is not None:
        return (EXIT_NOT_CLEAR, {"issue": held})

    # Reconcile before classifying (PROTOCOL.md §6). A dead worker's lease
    # obstructs exactly one party — the next worker who wants to claim — and that
    # is this one, so the repair belongs here rather than in an hourly cron. The
    # lease state came with the queue read, so the whole reconcile costs NOTHING
    # extra, and an expired lease costs no write at all: it is simply not held,
    # and the claim below steals it. The pass is repo-wide (not project-scoped)
    # because a lock is repo-wide, exactly like the cron it replaces.
    plan = reconcile_plan(read.tasks, leases, read.states)
    if plan:
        if apply_reconcile(api, plan, worker) is None:
            # Writes of ours may have half-landed — same state-unknown rule as a
            # failed claim: re-check before retrying, never push on.
            diag("claim-next: gh-failure stage=reconcile — state unknown, re-check")
            return (EXIT_TRANSPORT, None)
        project_reconcile(plan, read.tasks, leases, read.states)

    rows = queue.candidates(project, read=read)
    if rows is None:
        diag("claim-next: gh-failure stage=list")
        return (EXIT_TRANSPORT, None)

    for cand in rows:  # priority-first, then FIFO
        if not cand.startable:
            continue
        # Hand the claim the record and the lease this read already saw. The
        # record is what the guard re-derives "is it held" from, so it reaches
        # the same verdict as the filter that just offered the task instead of
        # refusing it — and the lease decides which generation the CAS starts
        # from, and whether this is a steal at all.
        record = read.record(cand.number)
        rc = claim_step(api, cand.number, worker, record=record,
                        lease=leases.get(cand.number, NO_LEASE), ttl=ttl)
        if rc == EXIT_OK:
            # The record as it stood when the task was offered — which is also
            # how it stands now, because a first claim writes none and a steal
            # only bumps the expiry count (§3.1).
            return (EXIT_OK, {"issue": cand.number, "title": cand.title,
                              "body": cand.body,
                              "bounced": record.requeued(cand.task.comment_total),
                              "pr": record.pr,
                              "anchor": record.comments})
        if rc == EXIT_TRANSPORT:
            # State is now ambiguous — do NOT move on to another candidate while
            # a write of ours may have half-landed. Re-check before any retry.
            diag(f"claim-next: gh-failure issue={cand.number} — state unknown, re-check")
            return (EXIT_TRANSPORT, None)
        # EXIT_LOST (10) / EXIT_NOT_CLEAR (11): back off, try the next candidate.

    diag(f"claim-next: none project:{project}")
    return (EXIT_NONE, None)


# --- subcommand: heartbeat ---------------------------------------------------

def cmd_heartbeat(args: argparse.Namespace) -> int:
    """Renew the lease: climb one generation with a fresh commit whose
    server-stamped date restarts the TTL and whose marker carries the progress
    text. No timeline comment — `status` surfaces the age and message from the
    ref. A worker holding a lease renews every TTL/3 (`contract lease-renew`).

    The renewal is a CAS, not an in-place update, and that is the point: a thief
    takes the task by creating the generation above the holder's, so a renewal
    and a steal compete to create the SAME ref and the server settles it. There
    is no interleaving in which a live worker is stolen from *and* believes it
    still holds the lease — the loser is told, immediately, with exit 10."""
    api, issue, worker, message = args.api, args.issue, args.worker, args.message
    refs = Refs(api)
    lost, head, refusal = refs.hold(issue, worker)
    if lost is not None:
        diag(f"heartbeat: {refusal}")
        return lost
    outcome, gen, _sha = refs.advance(
        issue, head.gen,
        {"type": "heartbeat", "worker": worker, "msg": message},
    )
    if outcome.startswith("fail"):
        diag(f"heartbeat: gh-failure issue={issue} stage={outcome[len('fail-'):]}")
        return EXIT_TRANSPORT
    if outcome == "lost":
        diag(f"heartbeat: lost-lease issue={issue} — another worker took the "
             "lease while this one was silent")
        return EXIT_LOST
    refs.drop(issue, head.superseded_below(gen))
    diag(f"heartbeat: renewed issue={issue} worker={worker}")
    return EXIT_OK


# --- subcommand: note --------------------------------------------------------

def cmd_note(args: argparse.Namespace) -> int:
    """Post a free-form worker comment (assumptions, a progress note) with the
    attribution disclaimer prepended and a single non-state-changing `note`
    marker (PROTOCOL.md §4) — the structural signal that makes the §6 requeue
    derivation read it as worker-authored. It carries no machine state: it changes no label and
    touches no claim ref, so the task stays exactly where it was — the missing
    worker-authored write the skill otherwise had you hand-assemble, disclaimer
    and all. next-action offers it as `then.note`."""
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
