"""Taking and releasing a task: the contended claim sequence and the
terminal transitions built on it.

Part of the kraken protocol package; see __init__.py."""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import time
from typing import Any, Callable, Sequence

from .contract import (
    EXIT_LOST, EXIT_NONE, EXIT_NOT_CLEAR, EXIT_OK, EXIT_TRANSPORT,
    EXIT_UNKNOWN_PROJECT, EXIT_USAGE, HELD_LABELS, Issue, Json, Worker, diag
)
from .comments import compose_comment, compose_note
from .transport import Api
from .lease import (
    Lease, NO_LEASE, clear_claim_state, format_age, lease_ttl_seconds,
    live_leases, refuse_second_claim, write_claim_state
)
from .refs import Refs
from .queue import Queue, requeued_labels
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
    *try*, never who wins."""
    head = Refs(api).head(issue)
    if not head.present:
        return head  # NO_LEASE or UNREADABLE_LEASE — the caller tells them apart
    # `claim_ref_head` cannot know the TTL, so it reports the clock and leaves
    # the verdict open; re-deciding `age`/`live` here against `now` is the whole
    # difference between the two.
    age = None if head.epoch is None else max(0, int(time.time() - head.epoch))
    return dataclasses.replace(head, age=age,
                               live=age is not None and age < ttl)


class ClaimAttempt:
    """One worker's attempt to take one task: guard, CAS, projection, executed
    identically every time (PROTOCOL.md §5). Shared by `claim` and `claim-next`
    so the two can never drift.

    The three phases were one 124-line function taking seven arguments, which is
    what state looks like when it has nowhere to live: they are not really
    parameters of a call, they are what an attempt IS. Each phase is a method
    that answers an exit code to stop with, or None to carry on; `run` sequences
    them and owns the one success path.

    Constructor arguments:

    `allow_held` names the held labels the caller's §6 requeue derivation has
    already lifted, so the guard does not refuse the very task the queue filter
    just offered. They are stale projection, and the projection phase swaps them
    off. The guard is an optimisation either way — the claim-ref CAS is the only
    thing that decides ownership.

    `lease` is the lease record the caller observed on this issue, or `NO_LEASE`
    when it observed no claim ref. It decides the generation the CAS starts
    from — a free task takes generation 1, and a task whose lease expired takes
    the one ABOVE its holder. That is the whole of what a "steal" is: not a
    different algorithm, a different starting rung.

    `probe_lease` is the named-claim path's substitute for that observation: a
    drain reads every lease with the queue, a `claim <issue>` has read nothing,
    so it looks the lease up itself — but only once the cheap label guard has
    already let the task through, so a task held by `needs-decision` or
    `awaiting-merge` still costs exactly one read to refuse."""

    def __init__(self, api: Api, issue: Issue, worker: Worker, *,
                 allow_held: Sequence[str] = (), lease: Lease = NO_LEASE,
                 ttl: int | None = None, probe_lease: bool = False):
        self.api = api
        self.refs = Refs(api)
        self.issue = issue
        self.worker = worker
        self.allow_held = allow_held
        self.lease = lease
        self.ttl = lease_ttl_seconds(ttl)
        self.probe_lease = probe_lease
        # Decided by the guard, consumed by the projection: whose expired lease
        # this is taking, and which requeued labels are still on the issue.
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

        The guard reads two labels, not three: `in-progress` is write-only (§3),
        so the lease below decides everything it used to. That collapses what was
        this sequence's hardest branch — a label that held only conditionally,
        probed mid-guard — into the plain rule the lock already stated."""
        label_names = self.api.issue_label_names(self.issue)
        if label_names is None:
            diag(f"claim: gh-failure issue={self.issue} stage=guard")
            return EXIT_TRANSPORT
        for held in HELD_LABELS:
            if held in label_names and held not in self.allow_held:
                diag(f"claim: held issue={self.issue} label={held}")
                return EXIT_NOT_CLEAR
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
        # somebody owns the task. Under protocol/5 the same case surfaced by
        # attempting the CAS and reading its 422; the generation ladder makes
        # that attempt worse than useless — creating the next generation would
        # SUCCEED and take a live lease — so the read answers it instead, with
        # the same verdict.
        if self.lease.held_by_other(self.worker):
            diag(f"claim: lost-cas issue={self.issue} — another worker holds the claim ref")
            return EXIT_LOST

        self.steal = self.lease if self.lease.stealable_by(self.worker) else NO_LEASE
        # The requeued labels still on the issue: dropped by the projection.
        self.stale_held = [h for h in HELD_LABELS
                           if h in label_names and h in self.allow_held]
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
        release the claim even if the writes below fail; then the in-progress
        label and claim comment. A failure here leaves the claim HELD by the
        lease — exit 20 says re-check. The label is the only thing that can be
        left behind, and nothing reads it (§3): the task stays correctly held,
        just without its badge, until the terminal transition writes the next
        one."""
        write_claim_state(self.api.repo, self.issue, self.worker)
        if self.steal.present and not self.api.post_comment(
                self.issue, lease_expired_body(self.worker, self.steal)):
            return self._held("comment")
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
                allow_held: Sequence[str] = (), lease: Lease = NO_LEASE,
                ttl: int | None = None, probe_lease: bool = False) -> int:
    """The claim sequence as a call. Kept because `acquire_next` injects it as
    `claim_step` and both subcommands reach it through here."""
    return ClaimAttempt(api, issue, worker, allow_held=allow_held, lease=lease,
                        ttl=ttl, probe_lease=probe_lease).run()


def cmd_claim(args: argparse.Namespace) -> int:
    refused = refuse_second_claim(args.worker, args.issue)
    if refused is not None:
        return refused
    # A named claim has read no queue, so it looks the lease up itself: an
    # expired one does not hold the task, it gets stolen (§5).
    return _claim_once(args.api, args.issue, args.worker, probe_lease=True)


# --- subcommand: claim-next --------------------------------------------------

def verify_project(api: Api, project: str) -> tuple[bool | None, str]:
    """Check that the coordination repo actually carries the `project:<name>`
    label this worker was pointed at. Returns (ok, message): True when the label
    exists, False (with a message naming the configured projects and the fix)
    when it does not, and None when the label read itself failed — a project is
    never declared missing from a read that never landed.

    Routing is entirely client-side (§3): a worker scoped to a label nobody uses
    filters every task out and reads as an empty queue forever, so a typo'd or
    never-created project silently produces a worker that hears nothing. Cheap to
    check once, so check it before the drain rather than never."""
    names = list_projects(api)
    if names is None:
        return (None, "project check: gh-failure stage=labels")
    if project in names:
        return (True, "")
    configured = ", ".join(names) if names else "(none configured)"
    return (False,
            "unknown project: %s has no `project:%s` label, so this worker would "
            "never see a task. Configured projects: %s. Fix the --project "
            "spelling, or create the label with `kraken.py init %s --project %s`."
            % (api.repo, project, configured, api.repo, project))


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
    """The whole acquisition — one-task-at-a-time guard, project preflight, queue
    read, §6 reconcile, then guard + CAS down the candidate list — as DATA rather
    than as printed output: returns `(exit_code, won)` where `won` is
    `{"issue", "title", "body"}` on EXIT_OK and None on every other outcome.

    `claim-next` and `next-action` are both thin renderings of this, which is
    what keeps the deterministic claim loop from drifting between them: there is
    one implementation and two presentations. Diagnostics go through `diag`, so
    `next-action` can push them to stderr and keep stdout pure JSON.

    `queue` and `claim_step` default to the real thing and exist so the
    ITERATION — skip-on-held, skip-on-lost, forward-only, stop-on-transport — can
    be tested against a scripted queue and scripted claim outcomes. Reading the
    queue and filtering it used to be two separate injections; they are one
    collaborator now, which is what `Queue` is for. Both have their own tests; a
    caller in production passes neither."""
    queue = queue or Queue(api)
    claim_step = claim_step or _claim_once
    refused = refuse_second_claim(worker)
    if refused is not None:
        return (refused, None)

    # Project preflight: before the queue is read and before any write,
    # because a worker scoped to a project label the repo does not carry is deaf,
    # not idle — it would report an honest-looking empty queue forever.
    ok, message = verify_project(api, project)
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
    nodes, leases = read

    # Reconcile before classifying (PROTOCOL.md §6). A dead worker's lease
    # obstructs exactly one party — the next worker who wants to claim — and that
    # is this one, so the repair belongs here rather than in an hourly cron. The
    # lease state came with the queue read, so the whole reconcile costs NOTHING
    # extra, and an expired lease costs no write at all: it is simply not held,
    # and the claim below steals it. The pass is repo-wide (not project-scoped)
    # because a lock is repo-wide, exactly like the cron it replaces.
    plan = reconcile_plan(nodes, leases)
    if plan:
        if apply_reconcile(api, plan, worker) is None:
            # Writes of ours may have half-landed — same state-unknown rule as a
            # failed claim: re-check before retrying, never push on.
            diag("claim-next: gh-failure stage=reconcile — state unknown, re-check")
            return (EXIT_TRANSPORT, None)
        project_reconcile(plan, nodes, leases)

    rows = queue.candidates(project, read=read)
    if rows is None:
        diag("claim-next: gh-failure stage=list")
        return (EXIT_TRANSPORT, None)

    by_number = {n["number"]: n for n in nodes}
    live = live_leases(leases)
    for cand in rows:  # priority-first, then FIFO
        if not cand.startable:
            continue
        # Re-derive the requeue verdict from the same node the filter used — a
        # pure call, no request — so the guard accepts a task an operator reply
        # has already requeued instead of refusing what was just offered.
        allow_held = requeued_labels(by_number[cand.number], live)
        # Hand the claim the lease this read already saw: it decides which
        # generation the CAS starts from, and whether this is a steal at all.
        rc = claim_step(api, cand.number, worker, allow_held=allow_held,
                       lease=leases.get(cand.number, NO_LEASE), ttl=ttl)
        if rc == EXIT_OK:
            return (EXIT_OK, {"issue": cand.number, "title": cand.title,
                              "body": cand.body})
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


# --- the terminal transitions: escalate, deliver, release --------------------

def read_body_file(path: str) -> str:
    """Read a file the way `$(cat file)` did: content with trailing newlines
    stripped (interior preserved)."""
    with open(path, encoding="utf-8") as fh:
        return fh.read().rstrip("\n")


@dataclasses.dataclass(frozen=True)
class Terminal:
    """What one terminal transition WRITES — the four things escalate, deliver
    and release disagree about. How they write it is the same six steps in the
    same order for all three, which is why that lives in `TerminalTransition`
    and this is a table."""

    command: str          # the subcommand name every diagnostic is prefixed with
    marker: str           # the comment's marker type (PROTOCOL.md §4)
    done: str             # the verb the success line reports
    add_label: str | None = None  # the operator-facing state the task lands on
    label_stage: str = "labels"   # the stage name a failed label swap reports
    clear_on_lost: bool = False   # also drop the state file on a lost lease


ESCALATE = Terminal("escalate", "needs-decision", "escalated",
                    add_label="needs-decision")
DELIVER = Terminal("deliver", "delivered", "delivered",
                   add_label="awaiting-merge")
# Release adds no label — stripping `in-progress` IS the transition, and the
# task rejoins the queue wearing nothing. `clear_on_lost` is not a symmetry to
# tidy away: a release whose lease turned out to be somebody else's must still
# drop the local claim state, or the caller that asked for it (a lifecycle hook,
# the ambush loop) retries forever a release that is no longer its business.
RELEASE = Terminal("release", "released", "released",
                   label_stage="label", clear_on_lost=True)


class TerminalTransition:
    """A transition that ends a claim: prove the lease, write the record, then
    release the lock — in that order, because a half-executed transition must
    leave the task HELD, never free with no record on the thread. A leftover ref
    is an orphan lock the reaper deletes; a freed task with nothing on its
    timeline is a task two workers can deliver.

    That ordering rule is the reason this is an object. It used to be stated
    three times, once per subcommand, and kept honest by a comment inside
    `deliver` that named `escalate` as the authority — a protocol invariant with
    no home. Changing the order is now one edit, and the three cannot drift.

    The three also had no collaborator, only a recipe for fabricating one:
    `Refs(api)` was rebuilt at each step of each transition. Here the gateway is
    `self.refs`, made once.

    Diagnostics go through `diag`, so a caller that owns stdout for a machine
    payload can route them without this knowing."""

    def __init__(self, api: Api, issue: Issue, worker: Worker,
                 terminal: Terminal):
        self.api = api
        self.refs = Refs(api)
        self.issue = issue
        self.worker = worker
        self.terminal = terminal

    def run(self, prose: str, *, payload: Json | None = None,
            suffix: str = "") -> int:
        """Execute the transition and return its exit code. `prose` is the
        human-facing comment body, `payload` the marker fields beyond
        `type`/`worker` (a PR url, a release reason), `suffix` whatever extra the
        success line reports."""
        t = self.terminal
        # The lease first (§5.3), before a single write. This is the case the
        # short TTL is built around: a worker that went silent long enough to be
        # stolen from may still finish and come back, and its write would land on
        # a task another worker is now executing — two deliveries, one task, a
        # question nobody is waiting on, or a release that deletes the NEW
        # holder's lock and strips the label out from under them. The work is not
        # lost: re-claiming the task writes it honestly.
        lost, head, refusal = self.refs.hold(self.issue, self.worker)
        if lost is not None:
            diag(f"{t.command}: {refusal}")
            if t.clear_on_lost and lost == EXIT_LOST:
                clear_claim_state(self.worker)
            return lost

        body = compose_comment(
            self.worker, prose,
            {"type": t.marker, "worker": self.worker, **(payload or {})})
        if not self.api.post_comment(self.issue, body):
            return self._failed("comment")
        if not self.api.swap_labels(self.issue, remove="in-progress",
                                    add=t.add_label):
            return self._failed(t.label_stage)
        # The record and the labels first, the lock LAST: the ref is the claim,
        # so deleting it is what actually frees the task — everything above is
        # narrative, and the task must never look free while half-released.
        if not self.refs.drop(self.issue, head.gens):
            return self._failed("ref")

        clear_claim_state(self.worker)
        diag(f"{t.command}: {t.done} issue={self.issue} "
             f"worker={self.worker}{suffix}")
        return EXIT_OK

    def _failed(self, stage: str) -> int:
        """A write that did not land. The lease is still ours and the task is
        still held — exit 20 says re-check, never push on."""
        diag(f"{self.terminal.command}: gh-failure issue={self.issue} "
             f"stage={stage}")
        return EXIT_TRANSPORT


def cmd_escalate(args: argparse.Namespace) -> int:
    api, issue, worker, question_file = args.api, args.issue, args.worker, args.question_file
    if not os.path.isfile(question_file):
        print(f"escalate: no such file {question_file}", file=sys.stderr)
        return EXIT_USAGE
    return TerminalTransition(api, issue, worker, ESCALATE).run(
        read_body_file(question_file))


def cmd_deliver(args: argparse.Namespace) -> int:
    api, issue, worker, result_file = args.api, args.issue, args.worker, args.result_file
    pr_url = args.pr_url
    if not os.path.isfile(result_file):
        print(f"deliver: no such file {result_file}", file=sys.stderr)
        return EXIT_USAGE

    prose = read_body_file(result_file)
    payload = {}
    if pr_url:
        payload["pr"] = pr_url
        prose = f"{prose}\n\nPR: {pr_url}"
    return TerminalTransition(api, issue, worker, DELIVER).run(
        prose, payload=payload, suffix=f" pr={pr_url}" if pr_url else "")


def cmd_release(args: argparse.Namespace) -> int:
    api, issue, worker, reason = args.api, args.issue, args.worker, args.reason
    prose = "Released this claim — the task rejoins the queue."
    payload = {}
    if reason:
        payload["reason"] = reason
        prose = f"{prose}\n\nReason: {reason}"
    return TerminalTransition(api, issue, worker, RELEASE).run(
        prose, payload=payload)


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


def list_projects(api: Api) -> list[str] | None:
    """Every project:<name> label configured in the repo, sorted, prefix
    stripped — the launch recon points a worker at each. Read from the repo's
    label set (not the open-task walk) so a project with no open task still gets
    a launch line. Returns a sorted name list, or None on transport failure."""
    items = api.paginated(f"/repos/{api.repo}/labels")
    if items is None:
        return None
    return sorted(
        n["name"][len("project:"):]
        for n in items
        if isinstance(n, dict) and str(n.get("name", "")).startswith("project:")
    )
