"""The state record: what a task IS between workers (PROTOCOL.md §3.1).

Part of the kraken protocol package; see __init__.py."""
from __future__ import annotations

import dataclasses
from typing import Iterable, Mapping

from .contract import (
    CommitMeta, HELD_LABELS, Issue, Json, Sha, Worker
)
from .comments import parse_marker
from .transport import Api
from .refs import EMPTY_TREE_SHA, Refs, kraken_ref_items

# --- the state record --------------------------------------------------------
# refs/kraken/state/<issue> points at an orphan commit whose message is a
# `state` marker (§4), so one read of refs/kraken/ returns every record together
# with the claim refs.

STATE_REF_PREFIX = "refs/kraken/state/"

# `in-progress` is absent on purpose: execution is the lease's business (§5).
QUEUED = "queued"
RECORD_STATES = (QUEUED,) + HELD_LABELS


def state_ref(issue: Issue) -> str:
    return f"{STATE_REF_PREFIX}{issue}"


def parse_state_ref(ref: str) -> Issue | None:
    """The issue number of a state ref, or None. Strict because matching-refs
    is a prefix match: `kraken/state/12` also returns `kraken/state/120`."""
    if not ref.startswith(STATE_REF_PREFIX):
        return None
    rest = ref[len(STATE_REF_PREFIX):]
    return int(rest) if rest.isdigit() else None


def state_ref_shas(items: Iterable[Json]) -> dict[Issue, Sha]:
    """`{issue: sha}` for every state ref in a matching-refs payload."""
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
    """One task's state record (PROTOCOL.md §3.1): an immutable observation.
    Writers build the next one with `moved_to` & co., which carry `expiries`
    forward. `NO_RECORD` and `UNREADABLE_RECORD` are its null objects."""

    state: str = QUEUED
    worker: Worker | None = None
    comments: int = 0           # the anchor §6 compares the live total against
    expiries: int = 0           # leases taken over, cumulative
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
        """The recorded state is a holding one — which is not `holds`: a held
        record whose thread moved on is requeued."""
        return self.state in HELD_LABELS

    def requeued(self, total: int | None) -> bool:
        """§6's requeue derivation: a comment arrived after this held record.

        An unreadable `total` (None) reads as requeued — failing toward "the
        thread moved on" costs one idle turn, the other way buries a reopened
        delivery. The GraphQL walk deliberately fails the other way (it floors a
        lost count to 0), since there one bad response covers the whole queue."""
        if not self.held_state:
            return False
        return True if total is None else total > self.comments

    def holds(self, total: int | None) -> bool:
        """Whether this record still holds the task against a live total."""
        return self.held_state and not self.requeued(total)

    def moved_to(self, state: str, worker: Worker, comments: int, *,
                 pr: str | None = None) -> TaskState:
        """The record a transition to `state` writes. `comments` is the count
        read back AFTER the transition's own comment landed (§3.1). `pr` is
        sticky: a later escalation must not erase where the work is."""
        return dataclasses.replace(
            self, state=state, worker=worker, comments=comments,
            pr=pr if pr is not None else self.pr,
            recorded=True, known=True, sha=None,
        )

    def re_anchored(self, comments: int) -> TaskState:
        """The same record with only its anchor moved. The hold stays: a deleted
        comment is not an answer."""
        return dataclasses.replace(self, comments=int(comments),
                                   recorded=True, known=True, sha=None)

    def stolen(self, worker: Worker) -> TaskState:
        """The record a steal writes (§5.2): one more expiry, the thief's name,
        the state untouched."""
        return dataclasses.replace(self, worker=worker,
                                   expiries=self.expiries + 1,
                                   recorded=True, known=True, sha=None)

    def payload(self) -> Json:
        """The `state` marker's fields (§4); `pr` is omitted, never null."""
        out: Json = {"type": "state", "state": self.state,
                     "worker": self.worker or "", "comments": int(self.comments),
                     "expiries": int(self.expiries)}
        if self.pr:
            out["pr"] = self.pr
        return out


# No ref: queued, no expiries. Safe to fail open on, because a held task with no
# record is still held by its label until §6's rule 3 writes one.
NO_RECORD = TaskState()

# The read did not land; no caller may write on the strength of it (§3.1).
UNREADABLE_RECORD = TaskState(known=False)


def parse_state_commit(message: str, sha: Sha | None = None) -> TaskState | None:
    """Decode a state ref's commit message, or None when it carries no usable
    `state` marker. Malformed or unknown states are ignored, never guessed (§4)."""
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
    """A counter off the wire; anything unusable reads as 0, which never
    escalates early."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


def holding_state(record: TaskState, total: int | None,
                  held_labels: Iterable[str] = ()) -> str | None:
    """The state holding a task, or None: §3.1's read rule. A record decides
    when there is one; otherwise a held label does, which is how a queue an
    older writer left behind still reads correctly until §6's rule 3 migrates
    it."""
    if not record.recorded:
        for label in HELD_LABELS:
            if label in held_labels:
                return label
        return None
    return record.state if record.holds(total) else None


def state_view(shas: Mapping[Issue, Sha],
               commit_meta: CommitMeta) -> dict[Issue, TaskState]:
    """`{issue: TaskState}` for a whole queue read. An undecodable record is
    dropped, which lets §6's rule 3 rewrite it from the label."""
    out: dict[Issue, TaskState] = {}
    for issue, sha in shas.items():
        entry = commit_meta.get(sha) or {}
        record = parse_state_commit(entry.get("message") or "", sha)
        if record is not None:
            out[issue] = record
    return out


class States:
    """The `refs/kraken/state/` namespace. Unlike `Refs` there is no ladder and
    no CAS: a record is simply written (§3.1)."""

    def __init__(self, api: Api):
        self.api = api

    # --- reading ---------------------------------------------------------------

    def all(self, items: Iterable[Json] | None = None,
            ) -> dict[Issue, Sha] | None:
        """Every state ref as `{issue: sha}`, or None on transport failure.
        Pass an already-fetched `refs/kraken/` payload as `items` to avoid a
        second paginated read."""
        if items is None:
            items = kraken_ref_items(self.api)
            if items is None:
                return None
        return state_ref_shas(items)

    def at(self, sha: Sha) -> TaskState:
        """The record behind a known sha: one commit read. A commit that does
        not decode is `NO_RECORD`, so §6's rule 3 can rewrite it."""
        meta = Refs(self.api).commit_meta([sha])
        if meta is None:
            return UNREADABLE_RECORD
        entry = meta.get(sha) or {}
        record = parse_state_commit(entry.get("message") or "", sha)
        return NO_RECORD if record is None else record

    def of(self, issue: Issue) -> TaskState:
        """One task's record: two calls, so hot paths take records from the
        bulk queue read instead. matching-refs is a prefix match, hence the
        parse — `state/12` must not answer with `state/120`."""
        if not str(issue).lstrip("-").isdigit():
            return UNREADABLE_RECORD
        items = self.api.paginated(
            f"/repos/{self.api.repo}/git/matching-refs/kraken/state/{int(issue)}")
        if items is None:
            return UNREADABLE_RECORD
        sha = state_ref_shas(items).get(int(issue))
        return NO_RECORD if sha is None else self.at(sha)

    # --- writing ---------------------------------------------------------------

    def write(self, issue: Issue, record: TaskState) -> bool:
        """Create the ref, or force-update it. Force is safe here, unlike on a
        claim ref: the only writers are the proven lease holder (§5.3) and the
        idempotent reconciler. Create comes first because a task's first
        transition is the common case."""
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
        """Delete a task's record; already-absent (422) counts as success."""
        status, _text = self.api.request(
            "DELETE", f"/repos/{self.api.repo}/git/{state_ref(issue)}")
        return 200 <= status < 300 or status == 422


__all__ = [
    "EMPTY_TREE_SHA", "NO_RECORD", "QUEUED", "RECORD_STATES",
    "STATE_REF_PREFIX", "States", "TaskState", "UNREADABLE_RECORD",
    "holding_state", "parse_state_commit", "parse_state_ref", "state_ref",
    "state_ref_shas", "state_view",
]
