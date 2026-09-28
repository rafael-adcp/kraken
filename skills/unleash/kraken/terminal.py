"""The terminal transitions: escalate, deliver, release (PROTOCOL.md §7-§9).

The three ways a worker's turn ENDS: what differs is a table (`Terminal`),
what is shared is one object (`TerminalTransition`).

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
from .transport import Api, TransportError
from .lease import clear_claim_state
from .refs import Refs
from .state import QUEUED, States

@dataclasses.dataclass(frozen=True)
class Terminal:
    """What one terminal transition writes."""

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
# Release adds no label and records `queued`, carrying `expiries` back (§3.1).
# `clear_on_lost`: a hook releasing a lease that is no longer ours must still
# drop the local state, or it retries forever.
RELEASE = Terminal("release", "released", "released", state=QUEUED,
                   label_stage="label", clear_on_lost=True)


class TerminalTransition:
    """A transition that ends a claim: prove the lease, write comment, record
    and labels, and release the lock LAST — so a half-executed transition leaves
    the task held, never free with no record. A leftover ref is an orphan lock
    the reconciler deletes."""

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
        """Execute the transition. `payload` holds marker fields beyond
        `type`/`worker`; `suffix` extends the success line."""
        t = self.terminal
        # The lease first (§5.3): a worker stolen from while silent must not
        # write onto the task its new holder is executing.
        hold = self.refs.hold(self.issue, self.worker)
        if hold.refused:
            diag(f"{t.command}: {hold.reason}")
            if t.clear_on_lost and hold.code == EXIT_LOST:
                clear_claim_state(self.worker)
            return hold.code

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
        if not self.refs.drop(self.issue, hold.head.gens):
            return self._failed("ref")

        clear_claim_state(self.worker)
        diag(f"{t.command}: {t.done} issue={self.issue} "
             f"worker={self.worker}{suffix}")
        return EXIT_OK

    def _record(self, pr: str | None = None) -> bool:
        """Write the record this transition lands on (§3.1), built from the
        previous one so `expiries` and `pr` carry over. A failed read fails the
        transition, leaving the task held by our lease."""
        try:
            total = self.api.comment_count(self.issue)
        except TransportError:
            return False
        previous = self.states.of(self.issue)
        if previous.unknown:
            return False
        return self.states.write(self.issue, previous.moved_to(
            self.terminal.state, self.worker, total, pr=pr))

    def _failed(self, stage: str) -> int:
        """A write that did not land; the task is still held, so re-check."""
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
