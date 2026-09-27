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
# The ref's commit date is the lease timestamp and the READER applies the
# expiry, so recovery from a dead worker takes one TTL with nothing installed.

# The worst-case time a dead worker's task sits unavailable. It must still span
# a live worker's longest silent step, since renewal happens every TTL/3.
LEASE_DEFAULT_TTL_SECONDS = 1800  # 30 minutes

# Derived, never configured: two renewals may be lost before anyone steals.
LEASE_RENEW_DIVISOR = 3

# Expiries on one task (the record's `expiries`) before §6 escalates it.
LEASE_EXPIRY_ESCALATE = 3


@dataclasses.dataclass(frozen=True)
class Lease:
    """What one task's claim-ref ladder says about who holds it, and until when:
    an immutable observation, aged once by `aged_at`. An unreadable clock is not
    live — it fails open toward the steal. `NO_LEASE` and `UNREADABLE_LEASE` are
    its null objects."""

    gen: Gen = LEGACY_CLAIM_GEN   # the highest generation seen — the holder's rung
    sha: Sha | None = None        # the commit that rung points at
    worker: Worker | None = None  # who the rung's marker names, None if unreadable
    epoch: Epoch | None = None    # the commit's server-stamped date; None = unreadable
    gens: tuple[Gen, ...] = ()    # every rung present, ascending
    # Decided by `aged_at`; until then, the fail-open defaults.
    age: int | None = None   # seconds since `epoch` at read time
    live: bool = False       # age is known AND below the TTL
    known: bool = True       # False only on UNREADABLE_LEASE

    @classmethod
    def from_ladder(cls, ladder: list[tuple[Gen, Sha]],
                    commit_meta: CommitMeta) -> Lease:
        """The un-aged lease a non-empty ladder describes: the highest rung is
        the holder, and its commit names the worker and the clock."""
        gen, sha = max(ladder)
        entry = commit_meta.get(sha) or {}
        payload = parse_marker(entry.get("message") or "") or {}
        return cls(gen=gen, sha=sha, worker=payload.get("worker") or None,
                   epoch=parse_iso(entry.get("committedDate") or ""),
                   gens=tuple(sorted(g for g, _s in ladder)))

    @property
    def present(self) -> bool:
        """Whether a claim ref was actually observed."""
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
        """Whether `worker` is on the holder's (highest) rung."""
        return self.present and self.worker == worker

    def held_by_other(self, worker: Worker) -> bool:
        """A live lease that is not ours: it holds the task whatever the labels
        say (§5)."""
        return self.live and self.worker != worker

    def stealable_by(self, worker: Worker) -> bool:
        """Somebody else's lease whose clock ran out: not held, so the claim
        below takes it by creating the generation above (§5.2)."""
        return self.expired and self.worker != worker

    def aged_at(self, now: Epoch, ttl: int) -> Lease:
        """This lease with `age` and `live` decided against `now`: the one
        place expiry is computed. A lease with no ladder answers itself."""
        if not self.present:
            return self
        age = None if self.epoch is None else max(0, int(now - self.epoch))
        return dataclasses.replace(self, age=age,
                                   live=age is not None and age < ttl)

    def superseded_below(self, gen: Gen) -> list[Gen]:
        """The rungs below `gen`: garbage once a reader has climbed past them."""
        return [g for g in self.gens if g < gen]


# No claim ref: unclaimed, and a claim starts from the legacy generation.
NO_LEASE = Lease()

# The read did not land: an absent ref means claim it, this means re-check.
UNREADABLE_LEASE = Lease(known=False)


def lease_ttl_seconds(explicit: int | None = None) -> int:
    """The explicit TTL, else KRAKEN_LEASE_TTL_SECONDS, else the default. A bad
    override falls back rather than expiring every live lease at once."""
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
    """Every lease of a queue read, aged."""
    return {issue: Lease.from_ladder(refs, commit_meta).aged_at(now, ttl)
            for issue, refs in claim_refs.items()}


def holder_shas(claim_refs: dict[Issue, list[tuple[Gen, Sha]]]) -> list[Sha]:
    """The holder SHA of each issue: the only commits whose date matters."""
    return [max(refs)[1] for refs in claim_refs.values() if refs]


def live_leases(leases: dict[Issue, Lease]) -> dict[Issue, Sha]:
    """{issue: sha} for the still-live leases."""
    return {issue: l.sha for issue, l in leases.items() if l.live}


# --- claim state file --------------------------------------------------------

def state_dir() -> str:
    return os.environ.get("KRAKEN_STATE_DIR") or os.path.join(
        os.path.expanduser("~"), ".kraken"
    )


def claim_state_path(worker: Worker) -> str:
    return os.path.join(state_dir(), f"claim-{worker}.json")


def agent_session() -> str | None:
    """The agent session id this process runs in, or None — the id the
    SessionEnd hook receives. Inside Copilot its own id wins: a `copilot`
    launched from Claude Code inherits CLAUDE_CODE_SESSION_ID, and recording that
    would let the Claude session's end release the Copilot session's claim."""
    if os.environ.get("COPILOT_CLI"):
        return os.environ.get("COPILOT_AGENT_SESSION_ID") or None
    return os.environ.get("CLAUDE_CODE_SESSION_ID") or None


def write_claim_state(repo: Repo, issue: Issue, worker: Worker) -> None:
    """Record the open claim for the SessionEnd hook to release. It names the
    session, because SessionEnd fires for every session on the host (#173).
    Best-effort: never worth failing a won claim over."""
    record = {"repo": repo, "issue": str(issue), "worker": worker}
    session = agent_session()
    if session:
        record["session"] = session
    d = state_dir()
    try:
        os.makedirs(d, exist_ok=True)
        with open(claim_state_path(worker), "w", encoding="utf-8") as fh:
            json.dump(record, fh)
            fh.write("\n")
    except OSError:
        pass


def clear_claim_state(worker: Worker) -> None:
    """Drop the claim state file on a terminal transition. Best-effort."""
    try:
        os.remove(claim_state_path(worker))
    except OSError:
        pass


def open_claim_record(worker: Worker) -> ClaimRecord | None:
    """The claim-<worker>.json record, or None. A hint for the hooks and the
    resume path, never the arbiter: the refs decide ownership."""
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
    """mtime of the flag the StopFailure hook stamps when a usage limit kills a
    turn, or None."""
    try:
        return os.path.getmtime(wake_retry_flag_path())
    except OSError:
        return None


def format_iso(epoch: Epoch) -> str:
    """Epoch seconds as ISO-8601 UTC (…Z); `parse_iso`'s inverse."""
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
    """A compact human age: '42s', '12m', '3h', '4d', or 'unknown'."""
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
