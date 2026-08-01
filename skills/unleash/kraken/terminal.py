"""The terminal transitions: escalate, deliver, release (PROTOCOL.md §7-§9).

The three ways a worker's turn on a task ENDS. They disagree about four things —
the comment marker, the state the record lands on, the label swapped in, the verb
the success line reports — and agree about everything else, so the disagreement is
a table (`Terminal`) and the agreement is one object (`TerminalTransition`).

Split out of `claim.py`, whose subject is the opposite half: taking a task and
holding it. Nothing here imports that module and nothing there imports this one —
they are siblings over the same refs, not layers.

Part of the kraken protocol package; see __init__.py."""
from __future__ import annotations

import argparse
import dataclasses
import os
import sys

from .contract import (
    EXIT_LOST, EXIT_OK, EXIT_TRANSPORT, EXIT_USAGE, Issue, Json, Worker, diag
)
from .comments import compose_comment, read_body_file
from .transport import Api
from .lease import clear_claim_state
from .refs import Refs
from .state import QUEUED, States

# --- the terminal transitions: escalate, deliver, release --------------------


@dataclasses.dataclass(frozen=True)
class Terminal:
    """What one terminal transition WRITES — the four things escalate, deliver
    and release disagree about. How they write it is the same six steps in the
    same order for all three, which is why that lives in `TerminalTransition`
    and this is a table."""

    command: str          # the subcommand name every diagnostic is prefixed with
    marker: str           # the comment's marker type (PROTOCOL.md §4)
    done: str             # the verb the success line reports
    state: str            # the state the record lands on (§3.1)
    add_label: str | None = None  # the operator-facing state the task lands on
    label_stage: str = "labels"   # the stage name a failed label swap reports
    clear_on_lost: bool = False   # also drop the state file on a lost lease


ESCALATE = Terminal("escalate", "needs-decision", "escalated",
                    state="needs-decision", add_label="needs-decision")
DELIVER = Terminal("deliver", "delivered", "delivered",
                   state="awaiting-merge", add_label="awaiting-merge")
# Release adds no label — stripping `in-progress` IS the transition, and the
# task rejoins the queue wearing nothing. It still writes a record, and it is the
# one transition that records `queued`: the task is going back to the queue, and
# the record is what carries its `expiries` there (§3.1). `clear_on_lost` is not
# a symmetry to tidy away: a release whose lease turned out to be somebody else's
# must still drop the local claim state, or the caller that asked for it (a
# lifecycle hook, the ambush loop) retries forever a release that is no longer
# its business.
RELEASE = Terminal("release", "released", "released", state=QUEUED,
                   label_stage="label", clear_on_lost=True)


class TerminalTransition:
    """A transition that ends a claim: prove the lease, write the record, then
    release the lock — in that order, because a half-executed transition must
    leave the task HELD, never free with no record on the thread. A leftover ref
    is an orphan lock the reaper deletes; a freed task with nothing on its
    timeline is a task two workers can deliver.

    That ordering rule is the reason this is an object: it is a protocol
    invariant, stated once here rather than once per subcommand, so changing the
    order is one edit and the three cannot drift. The ref gateway is `self.refs`,
    made once, rather than rebuilt at each step of each transition.

    Diagnostics go through `diag`, so a caller that owns stdout for a machine
    payload can route them without this knowing."""

    def __init__(self, api: Api, issue: Issue, worker: Worker,
                 terminal: Terminal):
        self.api = api
        self.refs = Refs(api)
        self.states = States(api)
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
        if not self._record((payload or {}).get("pr")):
            return self._failed("record")
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

    def _record(self, pr: str | None = None) -> bool:
        """Write the state record this transition lands the task on (§3.1) —
        between the comment and the labels, so the record is on the server before
        the claim ref that holds the task is deleted. A task freed with no record
        reads as queued while it is in fact delivered, which is the one ordering
        this protocol cannot tolerate.

        The comment count is read BACK from the issue, after this transition's
        own comment landed, rather than incremented from a count read earlier: a
        comment that arrived in between would otherwise be consumed silently and
        the operator's words lost.

        The previous record is read first because `expiries` is cumulative and
        `pr` is sticky (`TaskState.moved_to`) — a transition inherits the task's
        history rather than resetting it. Either read failing fails the
        transition, which leaves the task held by the lease this worker still
        owns: the correct place to be stuck."""
        total = self.api.comment_count(self.issue)
        if total is None:
            return False
        previous = self.states.of(self.issue)
        if previous.unknown:
            return False
        return self.states.write(self.issue, previous.moved_to(
            self.terminal.state, self.worker, total, pr=pr))

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
