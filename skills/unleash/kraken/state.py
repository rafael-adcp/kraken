"""The state record: what a task IS between workers (PROTOCOL.md §3.1).

Part of the kraken protocol package; see __init__.py."""
from __future__ import annotations

import dataclasses
import json
from typing import Iterable, Mapping

from .contract import (
    CommitMeta, HELD_LABELS, Issue, Json, Sha, Worker
)
from .comments import make_marker, parse_marker
from .transport import Api
from .refs import EMPTY_TREE_SHA, Refs, kraken_ref_items

# --- the state record --------------------------------------------------------
#
# refs/kraken/state/<issue> is where a task's operator-facing state lives: is it
# queued, waiting on a decision, or delivered and waiting for review. It points
# at an orphan commit whose message is a `state` marker (§4), so one paginated
# read of refs/kraken/ brings the whole queue's state back with the claim refs.
#
# It is NOT a compare-and-swap: a writer creates the ref when it is absent and
# force-updates it when it is present. The lease is what serializes writers
# (§5.3) — see HISTORY.md §3.1 for why a second lock would buy nothing.

STATE_REF_PREFIX = "refs/kraken/state/"

# The state a record may name. `in-progress` is deliberately absent: execution is
# the lease's business (§5), and a record says what the task is BETWEEN workers.
QUEUED = "queued"
RECORD_STATES = (QUEUED,) + HELD_LABELS


def state_ref(issue: Issue) -> str:
    return f"{STATE_REF_PREFIX}{issue}"


def parse_state_ref(ref: str) -> Issue | None:
    """The issue number of a state ref, or None for anything else. Strict for
    the same reason `parse_claim_ref` is: matching-refs is a plain PREFIX match,
    so asking for `kraken/state/12` also returns `kraken/state/120`."""
    if not ref.startswith(STATE_REF_PREFIX):
        return None
    rest = ref[len(STATE_REF_PREFIX):]
    return int(rest) if rest.isdigit() else None


def state_ref_shas(items: Iterable[Json]) -> dict[Issue, Sha]:
    """`{issue: sha}` for every state ref in a matching-refs payload, dropping
    anything that is not one. There is no generation ladder here — one ref per
    task, so the last writer's commit is the record."""
    out: dict[Issue, Sha] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        sha = (item.get("object") or {}).get("sha") or ""
        issue = parse_state_ref(item.get("ref") or "")
        if sha and issue is not None:
            out[issue] = sha
    return out


@dataclasses.dataclass(frozen=True)
class TaskState:
    """One task's state record — the answer every reader of "what is this task"
    goes through (PROTOCOL.md §3.1).

    Frozen for the same reason `Lease` is: a record is an *observation*. A reader
    that wants a fresh one re-reads the ref; a writer builds the next one with
    `moved_to`, which is what keeps `expiries` cumulative across transitions
    instead of something each caller remembers to carry forward.

    Three outcomes, all of them this one type, so no reader has to get a
    tri-state's fail-open direction right by hand:

      - a record was read (`recorded`) — its fields decide;
      - `NO_RECORD` — no ref at all, which reads as queued with no expiries. A
        task nobody has put down costs nothing and needs no migration;
      - `UNREADABLE_RECORD` — the read did not land (`unknown`), so no caller may
        write on the strength of it.

    Note which way the two absences fail. NO_RECORD says *queued*, which is
    fail-OPEN toward offering the task — safe, because a held task with no record
    is still held by its LABEL until §6's rule 3 writes one (see `Task.holding`).
    UNREADABLE_RECORD says nothing at all and every writer refuses on it."""

    state: str = QUEUED
    worker: Worker | None = None
    # The issue's total comment count at the moment of the transition — the
    # anchor §6 compares the live total against. Meaningless on a `queued`
    # record (the derivation ranges over held states only) but still written, so
    # no later transition has to invent it.
    comments: int = 0
    # How many times a lease on this task has expired and been taken over,
    # cumulative. Exact, unlike the marker count it replaces: a steal increments
    # it in the same pass that posts the `lease-expired` comment.
    expiries: int = 0
    pr: str | None = None       # the delivery URL (§8), None when there is none
    sha: Sha | None = None      # the commit this record was decoded from
    recorded: bool = False      # a ref was actually observed
    known: bool = True          # False only on UNREADABLE_RECORD

    @property
    def unknown(self) -> bool:
        """The read did not land — the caller owes an exit 20, never a write."""
        return not self.known

    @property
    def held_state(self) -> bool:
        """Whether the recorded state is one of the two that hold a task. Not
        the same question as `holds`: a held record whose thread has moved on is
        requeued, and holds nothing."""
        return self.state in HELD_LABELS

    def requeued(self, total: int | None) -> bool:
        """Whether a comment has arrived since this record was written — the
        whole of §6's requeue derivation, and the reason it costs no call: the
        total rides the queue walk, and this is the comparison.

        Only a held record can be requeued. A `queued` one is not holding
        anything to lift.

        `total` is None when the count could not be read — `comment_total_of`
        answers that for a REST issue object whose field is missing. It reads as
        REQUEUED, which is the fail-open direction: erring toward "the thread
        moved on" offers a task whose worker then finds nothing to do and
        escalates (§6), while erring the other way buries a delivery the
        operator asked to reopen and tells nobody.

        A GraphQL walk that loses the field does NOT get that treatment —
        `_comment_total` floors it to 0, which is below every anchor and so
        fails CLOSED, toward held. The asymmetry is deliberate and load-bearing:
        the walk reads the whole queue at once, so a fail-open there would offer
        every held task in the repo on one bad response. §6's re-anchor repair
        covers the other side, confirming over REST before it moves an anchor.

        This comparison is also memoryless on purpose. It cannot tell a thread
        that never had a reply from one whose comments were DELETED down past
        the anchor, and it must not: the anchor is what §6's repair fixes, not
        something a reader may reinterpret on the fly."""
        if not self.held_state:
            return False
        return True if total is None else total > self.comments

    def holds(self, total: int | None) -> bool:
        """Whether this record still holds the task against a live comment
        total. The single question the startable filter, the claim guard and the
        console all ask, so the derivation cannot drift into three copies."""
        return self.held_state and not self.requeued(total)

    def moved_to(self, state: str, worker: Worker, comments: int, *,
                 pr: str | None = None) -> TaskState:
        """The record a transition to `state` should write: the new state, the
        comment count read back AFTER its own comment landed (§3.1), and this
        worker — carrying `expiries` forward, because the count is cumulative
        over the task's whole life and a transition is not what resets it.

        `pr` is sticky on purpose: a delivery records one, and a later
        escalation on the same branch must not erase where the work is. Passing
        a new one replaces it."""
        return dataclasses.replace(
            self, state=state, worker=worker, comments=comments,
            pr=pr if pr is not None else self.pr,
            recorded=True, known=True, sha=None,
        )

    def re_anchored(self, comments: int) -> TaskState:
        """The record §6's re-anchor repair should write: everything exactly as
        it stands, with the `comments` anchor moved back onto a thread that
        SHRANK below it.

        Nothing else moves — not the state, not the worker, not the expiry
        count, not the PR. The hold is deliberately NOT lifted: a deleted
        comment is not an answer, and the repair only restores the *comparison*
        so the operator's next reply requeues instead of being absorbed by an
        anchor the thread can no longer reach."""
        return dataclasses.replace(self, comments=int(comments),
                                   recorded=True, known=True, sha=None)

    def stolen(self, worker: Worker) -> TaskState:
        """The record a steal should write: everything as it stood, with one
        more expiry (§5.2) and the thief's name on it. The state itself is
        untouched — taking over a lease does not change what the task is, only
        how many workers it has burned."""
        return dataclasses.replace(self, worker=worker,
                                   expiries=self.expiries + 1,
                                   recorded=True, known=True, sha=None)

    def payload(self) -> Json:
        """The `state` marker's fields (§4) — the wire form of this record.
        `pr` is omitted rather than written null when there is none, the same
        rule the comment markers follow."""
        out: Json = {"type": "state", "state": self.state,
                     "worker": self.worker or "", "comments": int(self.comments),
                     "expiries": int(self.expiries)}
        if self.pr:
            out["pr"] = self.pr
        return out


# No ref at all: the task is queued and has burned nobody. The null object every
# "what is this task" reader gets, so none of them spells `record is None` and
# gets the direction wrong.
NO_RECORD = TaskState()

# The read did not land. NOT recorded and NOT known: "this task is queued" is not
# what was observed, and no caller may write on the strength of it (§3.1).
UNREADABLE_RECORD = TaskState(known=False)


def parse_state_commit(message: str, sha: Sha | None = None) -> TaskState | None:
    """Decode a state ref's commit message into a `TaskState`, or None when it
    carries no usable `state` marker — a malformed marker is ignored, never
    guessed (§4), and a ref whose commit does not decode reads as no record at
    all rather than as some half-record.

    An unrecognized `state` value is refused for the same reason: a reader that
    accepted one would have to decide whether it holds, and there is no honest
    answer to that."""
    payload = parse_marker(message or "")
    if not payload or payload.get("type") != "state":
        return None
    state = payload.get("state")
    if state not in RECORD_STATES:
        return None
    pr = payload.get("pr")
    return TaskState(
        state=state,
        worker=payload.get("worker") or None,
        comments=_as_int(payload.get("comments")),
        expiries=_as_int(payload.get("expiries")),
        pr=pr.strip() if isinstance(pr, str) and pr.strip() else None,
        sha=sha,
        recorded=True,
    )


def _as_int(value: object) -> int:
    """A counter field off the wire, floored at 0. Foreign data: a missing,
    null or non-numeric count reads as 0 rather than crashing a queue read, and
    0 is the fail-open answer for both counters — it requeues (any comment is
    newer than none) and it never escalates early."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


def holding_state(record: TaskState, total: int | None,
                  held_labels: Iterable[str] = ()) -> str | None:
    """The state holding a task — `needs-decision`, `awaiting-merge`, or None
    when nothing is (PROTOCOL.md §3.1). The whole of §3.1's read rule, in one
    pure function, because three callers ask it from three different reads:
    the startable filter off the queue walk, the claim guard off one issue fetch,
    and the console off the same walk again.

    Where a record exists it decides and `held_labels` is ignored entirely. Where
    none does, a held label is honored — and no requeue is derivable for it,
    since there is no anchor to compare `total` against. That is the fallback
    that lets a protocol/9 reader read a queue an older writer left behind
    without ever mistaking a delivered task for a queued one; §6's rule 3 repairs
    it into a record on the first reconcile, and the requeue works from then on.

    The LEASE is not this question (§5), and no caller should pass it in here."""
    if not record.recorded:
        for label in HELD_LABELS:
            if label in held_labels:
                return label
        return None
    return record.state if record.holds(total) else None


def state_view(shas: Mapping[Issue, Sha],
               commit_meta: CommitMeta) -> dict[Issue, TaskState]:
    """`{issue: TaskState}` for a whole queue read: the state refs, decoded
    against the batched commit read the leases already paid for.

    A ref whose commit could not be read, or whose message carries no usable
    marker, is DROPPED rather than reported as a queued record. Absence is the
    fail-open answer here (see `TaskState`), and dropping is what makes a
    corrupted record self-healing: §6's rule 3 sees a held label with no record
    and writes a fresh one."""
    out: dict[Issue, TaskState] = {}
    for issue, sha in shas.items():
        entry = commit_meta.get(sha) or {}
        record = parse_state_commit(entry.get("message") or "", sha)
        if record is not None:
            out[issue] = record
    return out


class States:
    """The `refs/kraken/state/` namespace: reading a record, and writing one.

    Mirrors `Refs` deliberately — same shape, same `api`-on-the-object rule, same
    cheap construction — because they are two halves of one idea: the refs are
    where this protocol keeps machine state, and the queue reads them together.
    What is NOT mirrored is the ladder: a record has no generations and no CAS,
    so there is no `advance` here and no verdict to report (§3.1)."""

    def __init__(self, api: Api):
        self.api = api

    # --- reading ---------------------------------------------------------------

    def all(self, items: Iterable[Json] | None = None,
            ) -> dict[Issue, Sha] | None:
        """Every state ref as `{issue: sha}`, or None on transport failure.

        `items` is an already-fetched `refs/kraken/` payload — how a queue read
        gets the claim refs and the state records out of ONE paginated call
        rather than two. Omitting it fetches that payload here, for a caller that
        wants the records alone."""
        if items is None:
            items = kraken_ref_items(self.api)
            if items is None:
                return None
        return state_ref_shas(items)

    def at(self, sha: Sha) -> TaskState:
        """The record a sha already names — ONE commit read, for a caller that
        got the sha from a `refs/kraken/` payload it had already fetched.

        This is the half of `of` that is worth paying for on its own. Parsing the
        state refs out of that payload is pure (`state_ref_shas`), so a caller
        holding one knows which tasks have a record before spending anything;
        what it does not have is the commit behind it. Resolving exactly the one
        it cares about is how the §5 guard reads a record without the paginated
        ref read `of` would repeat — see `claim.open_claim_of`.

        `UNREADABLE_RECORD` only when the read did not land. A commit that does
        not decode reads as `NO_RECORD` — the same answer `of` and `state_view`
        give it, because a corrupt record is the case §6's rule 3 rewrites, and
        a reader that refused on it would block the repair instead."""
        meta = Refs(self.api).commit_meta([sha])
        if meta is None:
            return UNREADABLE_RECORD
        entry = meta.get(sha) or {}
        record = parse_state_commit(entry.get("message") or "", sha)
        return NO_RECORD if record is None else record

    def of(self, issue: Issue) -> TaskState:
        """One task's record — `NO_RECORD` when it has none, `UNREADABLE_RECORD`
        when the read did not land. Two calls (the ref, then its commit), which
        is why the queue read resolves the whole namespace in bulk instead and
        every hot path takes it from there.

        The ref read is a matching-refs prefix match, so it is filtered on the
        parsed name — `state/12` must not answer with `state/120`'s record."""
        if not str(issue).lstrip("-").isdigit():
            return UNREADABLE_RECORD
        items = self.api.paginated(
            f"/repos/{self.api.repo}/git/matching-refs/kraken/state/{int(issue)}")
        if items is None:
            return UNREADABLE_RECORD
        sha = state_ref_shas(items).get(int(issue))
        if sha is None:
            return NO_RECORD
        meta = Refs(self.api).commit_meta([sha])
        if meta is None:
            return UNREADABLE_RECORD
        entry = meta.get(sha) or {}
        record = parse_state_commit(entry.get("message") or "", sha)
        return NO_RECORD if record is None else record

    # --- writing ---------------------------------------------------------------

    def write(self, issue: Issue, record: TaskState) -> bool:
        """Write `record` as the task's state, creating the ref or force-updating
        it. True on success.

        Force is correct here and would be wrong on a claim ref: there is no
        ladder to climb and no race to arbitrate, because the only writers are
        the lease holder (which §5.3 made prove itself) and the reconciler
        (whose rules are idempotent). Two of them write the same record and
        converge.

        Create-then-update rather than update-then-create so the common case —
        the first transition of a task's life — is one call."""
        sha = Refs(self.api).commit(record.payload())
        if sha is None:
            return False
        status, _text = self.api.request(
            "POST", f"/repos/{self.api.repo}/git/refs",
            {"ref": state_ref(issue), "sha": sha},
        )
        if 200 <= status < 300:
            return True
        if status != 422:  # 422 is "it already exists" — anything else is real
            return False
        status, _text = self.api.request(
            "PATCH", f"/repos/{self.api.repo}/git/{state_ref(issue)}",
            {"sha": sha, "force": True},
        )
        return 200 <= status < 300

    def delete(self, issue: Issue) -> bool:
        """Delete a task's record. An already-absent ref (HTTP 422) counts as
        success, so the sweep stays idempotent under retries — the same
        tolerance `Refs.delete` applies to a claim ref."""
        status, _text = self.api.request(
            "DELETE", f"/repos/{self.api.repo}/git/{state_ref(issue)}")
        return 200 <= status < 300 or status == 422


# Re-exported so a caller that writes a record does not have to know a commit
# needs a tree: `States.write` is the whole surface.
__all__ = [
    "EMPTY_TREE_SHA", "NO_RECORD", "QUEUED", "RECORD_STATES",
    "STATE_REF_PREFIX", "States", "TaskState", "UNREADABLE_RECORD",
    "holding_state", "parse_state_commit", "parse_state_ref", "state_ref",
    "state_ref_shas", "state_view",
]
