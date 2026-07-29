"""What a claim holds and for how long — the Lease object, the TTL, the
clock helpers, and the local state file recording an open claim.

Part of the kraken protocol package; see __init__.py."""
from __future__ import annotations

import dataclasses
import datetime
import json
import os

from .contract import (
    ClaimRecord, CommitMeta, Epoch, Gen, Issue,
    LEGACY_CLAIM_GEN, Repo, Sha, Worker
)
from .comments import parse_marker

# --- the lease: how long a claim ref holds -----------------------------------
#
# The claim ref is a LEASE, not a lock that holds forever: the ref's commit date
# is the lease timestamp, the TTL below is how long that timestamp holds, and the
# party that applies the expiry is the READER — no scheduled job, no new state,
# no extra request (the date is already in hand from the queue read). Recovery
# from a dead worker therefore takes at most one TTL, in every harness, with
# nothing installed.

# The lease TTL in seconds. Minutes, not hours: this is the worst-case time a
# dead worker's task sits unavailable. It is bounded below by what a LIVE worker
# can honour — a worker renews every TTL/3, so the TTL must comfortably span a
# long silent step (a build, a test suite, one slow tool call) or a live worker
# would lose a lease it is still working under.
LEASE_DEFAULT_TTL_SECONDS = 1800  # 30 minutes

# Renewal cadence is DERIVED, never configured separately: a worker renews every
# TTL/3, so two consecutive renewals may be lost before anyone steals the lease.
LEASE_RENEW_DIVISOR = 3

# How many expiries on ONE task turn the steal into an escalation. A task that
# kills every worker that touches it would otherwise circulate forever, each
# steal burning another drain; past this count the operator hears about it
# instead (§6). Counted from the `lease-expired` markers already in the queue
# read's comment window, so the guard costs nothing.
LEASE_EXPIRY_ESCALATE = 3


@dataclasses.dataclass(frozen=True)
class Lease:
    """What one task's claim-ref ladder says about who holds it, and until when.

    Every reader — the startable filter, the reconciler, the claim path, the
    status console — decides from this one object, so expiry cannot be computed
    two ways. Built by `lease_state` from the ladder plus the batched commit
    read, and by `claim_ref_head` for a single issue.

    Frozen because a lease is an *observation* of a moment: `now` is baked into
    `age` and `live` when it is built. A reader that wants a fresh verdict
    re-reads the ref; it never edits the answer in place.

    `epoch`/`age` are None when the ref's commit (or its date) could not be
    read, and such a lease is **not live** — nothing proves the holder alive, so
    it fails OPEN toward the steal rather than holding the task forever behind
    an unreadable clock.

    A lease read has THREE outcomes, and all three are this one type: a ladder
    was there (`present`), no ref was there at all (`NO_LEASE`), or the read did
    not land (`UNREADABLE_LEASE`, `unknown`). They are objects rather than
    None-and-a-sentinel so that every reader can ask its question straight —
    `stealable_by`, `held_by_other` — and get the fail-open answer for free
    instead of re-deriving it from a tri-state it might get backwards.
    """

    gen: Gen = LEGACY_CLAIM_GEN   # the highest generation seen — the holder's rung
    sha: Sha | None = None        # the commit that rung points at
    worker: Worker | None = None  # who the rung's marker names, None if unreadable
    epoch: Epoch | None = None    # the commit's server-stamped date; None = unreadable
    gens: tuple[Gen, ...] = ()    # every rung present, ascending — the lower ones
                                  # are superseded and a reader may collect them
    # The expiry verdict, and it is only meaningful once a reader has applied a
    # TTL. `claim_ref_head` reports ownership and the clock but knows no TTL, so
    # it leaves these at the defaults; `lease_state` and `probe_lease_state`
    # decide them. The defaults are the fail-open answer — an undecided lease is
    # not evidence anybody is alive — and §5.3 never consults `live` anyway: an
    # expired lease that is still OURS may finish its transition.
    age: int | None = None   # seconds since `epoch` at read time
    live: bool = False       # age is known AND below the TTL
    # False only on UNREADABLE_LEASE: the read did not land, so "nobody holds
    # this" is NOT what was observed and no caller may write on the strength of
    # it. Kept apart from `present` because the two demand opposite answers —
    # an absent ref means claim it, an unknown one means re-check.
    known: bool = True

    @property
    def present(self) -> bool:
        """Whether a claim ref was actually observed. Derived from `gens` rather
        than stored: a real read always names at least the holder's rung, and
        the two no-ladder objects below carry none."""
        return bool(self.gens)

    @property
    def unknown(self) -> bool:
        """The read did not land — the caller owes an exit 20, never a write."""
        return not self.known

    @property
    def expired(self) -> bool:
        """A ladder is there and its clock has run out. Nobody is proven alive,
        so the next reader steals it; this is not the same as unheld."""
        return self.present and not self.live

    def held_by(self, worker: Worker) -> bool:
        """Whether `worker` is the one on the holder's rung. Ownership is the
        HIGHEST generation (see `hold_lease`), which is what `worker` names."""
        return self.present and self.worker == worker

    def held_by_other(self, worker: Worker) -> bool:
        """Somebody else is working on this, right now. A LIVE lease that is not
        ours holds the task whatever the labels say (§5) — including in the crash
        window where no label has landed yet."""
        return self.live and self.worker != worker

    def stealable_by(self, worker: Worker) -> bool:
        """Somebody else's lease whose clock ran out: not held, so the claim
        below takes it by creating the generation above (§5.2)."""
        return self.expired and self.worker != worker

    def aged_at(self, now: Epoch, ttl: int) -> Lease:
        """This lease with its clock decided against `now`: the same record, with
        `age` and `live` filled in. A read carrying no ladder (NO_LEASE,
        UNREADABLE_LEASE) answers itself — there is nothing to age, and both must
        keep their fail-open defaults.

        `Refs.head` reports ownership and the raw clock but knows no TTL, so it
        leaves the verdict open; this is where the verdict is made. `lease_state`
        makes the same one for a whole queue read, through here, so a single
        probe's expiry rule and the queue's cannot drift into two copies of the
        arithmetic."""
        if not self.present:
            return self
        age = None if self.epoch is None else max(0, int(now - self.epoch))
        return dataclasses.replace(self, age=age,
                                   live=age is not None and age < ttl)

    def superseded_below(self, gen: Gen) -> list[Gen]:
        """The rungs this reader has climbed past, which are garbage now — the
        holder is the highest one, so a delete that fails leaves a stray ref,
        never a second lease."""
        return [g for g in self.gens if g < gen]


# No claim ref at all: the task is unclaimed, and a claim starts from the legacy
# generation. The null object every "is it free?" reader gets, so none of them
# has to spell `lease is None` and get the fail-open direction wrong.
NO_LEASE = Lease()

# The read did not land. NOT live, NOT present, and NOT known: no caller may
# conclude the task is free from a read that never happened (§5.3).
UNREADABLE_LEASE = Lease(known=False)


def lease_ttl_seconds(explicit: int | None = None) -> int:
    """The lease TTL in seconds: the explicit value if given, else the
    KRAKEN_LEASE_TTL_SECONDS environment override, else the default. A
    non-integer or non-positive override falls back rather than crashing the
    pass — or worse, expiring every live lease at once."""
    if explicit is not None:
        return explicit
    try:
        ttl = int(os.environ.get("KRAKEN_LEASE_TTL_SECONDS", LEASE_DEFAULT_TTL_SECONDS))
    except ValueError:
        return LEASE_DEFAULT_TTL_SECONDS
    return ttl if ttl > 0 else LEASE_DEFAULT_TTL_SECONDS


def lease_renew_seconds(ttl: int | None = None) -> int:
    """How often a worker holding a lease must renew it: TTL/3."""
    return (lease_ttl_seconds() if ttl is None else ttl) // LEASE_RENEW_DIVISOR


def lease_state(
    claim_refs: dict[Issue, list[tuple[Gen, Sha]]],
    commit_meta: CommitMeta,
    now: Epoch,
    ttl: int,
) -> dict[Issue, Lease]:
    """The lease view of a queue read, as a PURE function — the one place expiry
    is decided, so every reader (the startable filter, the reconciler, the claim
    path, the status console) applies the same clock.

    `claim_refs` is {issue: [(generation, sha), …]} — the holder is the HIGHEST
    generation, and the lower ones are superseded refs a reader may collect. See
    `Lease` for what each field means, including why an unreadable clock is not
    live. The expiry verdict itself is `Lease.aged_at`, so this decides nothing a
    single-issue probe would decide differently."""
    leases = {}
    for issue, refs in claim_refs.items():
        gen, sha = max(refs)
        entry = commit_meta.get(sha) or {}
        payload = parse_marker(entry.get("message") or "") or {}
        leases[issue] = Lease(
            gen=gen,
            sha=sha,
            worker=payload.get("worker") or None,
            epoch=parse_iso(entry.get("committedDate") or ""),
            gens=tuple(sorted(g for g, _s in refs)),
        ).aged_at(now, ttl)
    return leases


def holder_shas(claim_refs: dict[Issue, list[tuple[Gen, Sha]]]) -> list[Sha]:
    """Only the SHAs the lease clock is read from — the highest generation of
    each issue. A superseded generation's commit date decides nothing, so the
    batched commit read stays one entry per claimed task, not per ref."""
    return [max(refs)[1] for refs in claim_refs.values() if refs]


def live_leases(leases: dict[Issue, Lease]) -> dict[Issue, Sha]:
    """Just the still-held leases, in the {issue: sha} shape the readers that
    only ask "is this task locked?" already consume."""
    return {issue: l.sha for issue, l in leases.items() if l.live}


# --- claim state file --------------------------------------------------------

def state_dir() -> str:
    return os.environ.get("KRAKEN_STATE_DIR") or os.path.join(
        os.path.expanduser("~"), ".kraken"
    )


def claim_state_path(worker: Worker) -> str:
    return os.path.join(state_dir(), f"claim-{worker}.json")


def write_claim_state(repo: Repo, issue: Issue, worker: Worker) -> None:
    """Record the open claim so the SessionEnd hook can auto-release it if the
    worker's session ends before a terminal transition. Best-effort: a state dir
    we cannot write is never worth failing a won claim over — the reaper backs
    us up regardless."""
    d = state_dir()
    try:
        os.makedirs(d, exist_ok=True)
        with open(claim_state_path(worker), "w", encoding="utf-8") as fh:
            json.dump({"repo": repo, "issue": str(issue), "worker": worker}, fh)
            fh.write("\n")
    except OSError:
        pass


def clear_claim_state(worker: Worker) -> None:
    """Drop the claim state file on a terminal transition (deliver / escalate /
    release), so a later graceful exit does not re-release a claim we no longer
    hold. Best-effort."""
    try:
        os.remove(claim_state_path(worker))
    except OSError:
        pass


def open_claim_record(worker: Worker) -> ClaimRecord | None:
    """The whole claim-<worker>.json record — `{"repo", "issue", "worker"}` with
    `issue` normalized to a string — or None when this worker holds no open
    claim. Every terminal transition (deliver / escalate / release) removes the
    file, so a resolved claim leaves nothing behind.

    The file is a HINT, never the arbiter: the §5 one-task-at-a-time guard is
    derived from the claim refs themselves (`claim.refuse_second_claim`), so a
    missing, unreadable, or malformed file just means no hint — it can neither
    brick this worker nor let it take a second task. What still reads it: the
    SessionEnd/StopFailure hooks and the loop's release-on-exit (which must work
    without another queue read), and `next-action`'s resume path — the record
    names the repo the claim was made in, because resuming a claim means proving
    the lease there, not on whichever repo this invocation happens to name."""
    try:
        with open(claim_state_path(worker), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    issue = data.get("issue")
    if issue is None:
        return None
    return {
        "repo": str(data.get("repo") or ""),
        "issue": str(issue),
        "worker": str(data.get("worker") or worker),
    }


def wake_retry_flag_path() -> str:
    return os.path.join(state_dir(), "wake-retry")


def wake_retry_mtime() -> float | None:
    """mtime of the wake-retry flag the StopFailure hook stamps when a usage
    limit kills a turn on this machine (hooks/stop-failure-release.sh), or None
    when no flag exists. The watcher compares it against its own last emission
    to decide whether a wake it spent was consumed by a dead turn."""
    try:
        return os.path.getmtime(wake_retry_flag_path())
    except OSError:
        return None


def format_iso(epoch: Epoch) -> str:
    """Epoch seconds as an ISO-8601 UTC timestamp (…Z) — `parse_iso`'s inverse,
    so a timestamp this program emits is one it can read back."""
    return datetime.datetime.fromtimestamp(
        epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(ts: str) -> Epoch | None:
    """An ISO-8601 UTC timestamp (…Z) to epoch seconds, or None if unparseable."""
    if not ts:
        return None
    try:
        dt = datetime.datetime.strptime(ts.strip(), "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return dt.replace(tzinfo=datetime.timezone.utc).timestamp()


def format_age(seconds: int | None) -> str:
    """A compact human age: '42s', '12m', '3h', '4d'. 'unknown' when there is no
    anchor (a worker that never left a liveness marker)."""
    if seconds is None:
        return "unknown"
    seconds = int(seconds)
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h"
    return f"{hours // 24}d"
