"""The claim-ref CAS: the generation ladder that decides who owns a task.

Part of the kraken protocol package; see __init__.py."""
from __future__ import annotations

import dataclasses
import json
from typing import Iterable, Sequence

from .contract import (
    CommitMeta, EXIT_LOST, EXIT_OK, EXIT_TRANSPORT, Gen, Issue, Json,
    LEGACY_CLAIM_GEN, Sha, Worker
)
from .comments import make_marker
from .transport import Api, TransportError
from .lease import Lease, NO_LEASE, UNREADABLE_LEASE

# --- claim refs: the CAS ladder, and the lease it carries --------------------
#
# The claim of issue N is a git ref refs/kraken/claims/N/<generation>. Creating a
# ref is the one common GitHub write that FAILS on conflict (422 to all but one
# creator), so the ref is the arbiter and the loser writes nothing. It points at
# an orphan commit whose message is the kraken marker and whose server-stamped
# date is the LEASE TIMESTAMP (§5): the ref does not hold forever — a reader that
# finds it older than the TTL treats it as expired.
#
# THE GENERATION IS WHAT MAKES THE STEAL A REAL CAS. The holder of a task is
# whoever owns its HIGHEST generation, and the only way to become the holder is
# to CREATE the next one. So every contended operation — first claim, steal,
# renewal — is the same conflict-failing primitive, racing on the same ref name:
#
#   free task            -> create N/1
#   steal an expired G   -> create N/(G+1)
#   renew your own G     -> create N/(G+1), then drop G
#
# Nothing is ever deleted to make room, so a thief and the renewing holder race
# on the identical ref and the server picks one. Deleting a superseded generation
# is garbage collection: losing that delete costs a stray ref, never a lease.

CLAIM_REF_PREFIX = "refs/kraken/claims/"
# Claims and state records (§3.1) share it, so one read answers both.
KRAKEN_REF_NAMESPACE = "kraken/"
# git's well-known empty-tree object, present in every repo, so an orphan commit
# needs no prior read; create_claim_commit falls back to HEAD's tree if a host
# rejects it.
EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def kraken_ref_items(api: Api) -> list[Json]:
    """Every ref under `refs/kraken/`, undecoded. `Refs.all` and `States.all` each parse their own family out of it."""
    return api.paginated(f"/repos/{api.repo}/git/matching-refs/{KRAKEN_REF_NAMESPACE}")


def claim_ref(issue: Issue, gen: Gen) -> str:
    if gen == LEGACY_CLAIM_GEN:
        return f"{CLAIM_REF_PREFIX}{issue}"
    return f"{CLAIM_REF_PREFIX}{issue}/{gen}"


def parse_claim_ref(ref: str) -> tuple[Issue, Gen] | None:
    """`(issue, generation)` for a claim ref name, or None. `…/claims/12` is the
    protocol/5 shape, read as generation 0. Strict because matching-refs is a
    prefix match: `kraken/claims/12` also returns `kraken/claims/120/1`."""
    if not ref.startswith(CLAIM_REF_PREFIX):
        return None
    parts = ref[len(CLAIM_REF_PREFIX):].split("/")
    if len(parts) == 1 and parts[0].isdigit():
        return (int(parts[0]), LEGACY_CLAIM_GEN)
    if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
        return (int(parts[0]), int(parts[1]))
    return None


def _parse_ref_items(items: Iterable[Json]) -> list[tuple[Issue, Gen, Sha]]:
    """[(issue, gen, sha)] for every claim ref in a matching-refs payload."""
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        sha = (item.get("object") or {}).get("sha") or ""
        parsed = parse_claim_ref(item.get("ref") or "")
        if sha and parsed is not None:
            out.append((parsed[0], parsed[1], sha))
    return out


@dataclasses.dataclass(frozen=True)
class Advance:
    """What climbing to generation `gen` came to: "won", "lost", or
    "fail-<stage>" when the write did not land and the state is unknown."""

    verdict: str
    gen: Gen

    @property
    def won(self) -> bool:
        return self.verdict == "won"

    @property
    def lost(self) -> bool:
        return self.verdict == "lost"

    @property
    def failed(self) -> bool:
        return self.verdict.startswith("fail-")

    @property
    def stage(self) -> str:
        return self.verdict[len("fail-"):]


@dataclasses.dataclass(frozen=True)
class Hold:
    """The write-after-expiry check's answer: the lease `head`, and when the
    caller may not write, the exit `code` and the `reason` it reports."""

    head: Lease
    code: int = EXIT_OK
    reason: str = ""

    @property
    def refused(self) -> bool:
        return self.code != EXIT_OK


class Refs:
    """One repo's claim-ref ladder, the CAS that arbitrates it, and the lease it
    carries (PROTOCOL.md §4/§5). Cheap to construct where needed."""

    def __init__(self, api: Api):
        self.api = api

    # --- reading the ladder ---------------------------------------------------

    def all(self, items: Iterable[Json] | None = None,
            ) -> dict[Issue, list[tuple[Gen, Sha]]]:
        """Every claim ref as {issue: [(generation, sha), …]}, uncollapsed.
        `items` reuses an already-fetched payload."""
        if items is None:
            items = kraken_ref_items(self.api)
        refs = {}
        for issue, gen, sha in _parse_ref_items(items):
            refs.setdefault(issue, []).append((gen, sha))
        return refs

    def of(self, issue: Issue) -> list[tuple[Gen, Sha]]:
        """One issue's sorted ladder; [] when unclaimed. A malformed issue
        number is unreadable, never unclaimed."""
        if not str(issue).lstrip("-").isdigit():
            raise TransportError()
        items = self.api.paginated(
            f"/repos/{self.api.repo}/git/matching-refs/kraken/claims/{int(issue)}"
        )
        return sorted((gen, sha) for i, gen, sha in _parse_ref_items(items)
                      if i == int(issue))

    def commit_meta(self, shas: Sequence[Sha]) -> CommitMeta:
        """{sha: {committedDate, message}} in one aliased fan-out. Aliases are indexes (`c0`…) because a GraphQL alias
        must be a name and a SHA may start with a digit."""
        ordered = sorted(set(shas))
        fields = [
            f'c{i}: object(oid: "{sha}") {{ ... on Commit {{ committedDate message }} }}'
            for i, sha in enumerate(ordered)
        ]
        repo_obj = self.api.aliased(fields)
        meta = {}
        for i, sha in enumerate(ordered):
            obj = repo_obj.get(f"c{i}") or {}
            meta[sha] = {
                "committedDate": obj.get("committedDate") or "",
                "message": obj.get("message") or "",
            }
        return meta

    def head(self, issue: Issue) -> Lease:
        """The head of one issue's lease: the highest generation, whose it is,
        and every generation present. `NO_LEASE` when unclaimed,
        `UNREADABLE_LEASE` when the read did not land. Not aged: it knows no
        TTL."""
        try:
            refs = self.of(issue)
            if not refs:
                return NO_LEASE
            return Lease.from_ladder(refs, self.commit_meta([max(refs)[1]]))
        except TransportError:
            return UNREADABLE_LEASE

    def owner(self, issue: Issue) -> Worker | None:
        """The worker holding `issue`, or None when absent or unreadable."""
        return self.head(issue).worker

    # --- writing the ladder ---------------------------------------------------

    def commit(self, payload: Json) -> Sha:
        """The orphan commit a ref points at: empty tree, no parents, the marker
        as message. The server stamps the date."""
        for tree in (EMPTY_TREE_SHA, None):
            if tree is None:
                tree = self._head_tree_sha()
            status, text = self.api.request(
                "POST", f"/repos/{self.api.repo}/git/commits",
                {"message": make_marker(payload), "tree": tree, "parents": []},
            )
            if 200 <= status < 300:
                try:
                    sha = json.loads(text).get("sha")
                except (ValueError, json.JSONDecodeError):
                    raise TransportError() from None
                if not isinstance(sha, str) or not sha:
                    raise TransportError()
                return sha
            if status != 422:
                raise TransportError()
            # 422 on the empty tree: this host wants a reachable tree — fall back.
        raise TransportError()

    def create(self, issue: Issue, gen: Gen, sha: Sha) -> str:
        """The CAS: create generation `gen`. "won", "lost" (422: somebody else
        created it first) or "fail" (transport, state unknown)."""
        status, _text = self.api.request(
            "POST", f"/repos/{self.api.repo}/git/refs",
            {"ref": claim_ref(issue, gen), "sha": sha},
        )
        if 200 <= status < 300:
            return "won"
        if status == 422:
            return "lost"
        return "fail"

    def delete(self, issue: Issue, gen: Gen) -> bool:
        """Delete one generation; already-missing (422) counts as success."""
        status, _text = self.api.request(
            "DELETE", f"/repos/{self.api.repo}/git/{claim_ref(issue, gen)}"
        )
        return 200 <= status < 300 or status == 422

    def drop(self, issue: Issue, gens: Iterable[Gen]) -> bool:
        """Delete a set of generations; True only if all went. A leftover one is
        untidy, never a held lease."""
        ok = True
        for gen in gens:
            if not self.delete(issue, gen):
                ok = False
        return ok

    def advance(self, issue: Issue, gen: Gen, payload: Json) -> Advance:
        """Create the generation above `gen` (§5.2)."""
        try:
            sha = self.commit(payload)
        except TransportError:
            return Advance("fail-commit", gen + 1)
        verdict = self.create(issue, gen + 1, sha)
        return Advance("fail-ref" if verdict == "fail" else verdict, gen + 1)

    def hold(self, issue: Issue, worker: Worker) -> Hold:
        """The write-after-expiry check (§5.3): prove this worker still holds
        the lease before a transition writes anything.

        This is what makes a short TTL safe: a worker that stalled long enough
        to be stolen from writes nothing. Ours-but-expired still passes — nobody
        took it, and the question is who holds the lease, not its age."""
        head = self.head(issue)
        if head.unknown:
            return Hold(head, EXIT_TRANSPORT, f"gh-failure issue={issue} stage=lease")
        if not head.present:
            return Hold(head, EXIT_LOST,
                        f"lost-lease issue={issue} — the lease is gone, "
                        "re-claim the task before writing to it")
        if not head.held_by(worker):
            holder = head.worker or "another worker"
            return Hold(head, EXIT_LOST,
                        f"lost-lease issue={issue} — the lease is held by {holder}")
        return Hold(head)

    # --- internals ------------------------------------------------------------

    def _head_tree_sha(self) -> Sha:
        """HEAD's tree SHA, for hosts that reject the empty tree."""
        obj = self.api.json("GET", f"/repos/{self.api.repo}/commits/HEAD")
        tree = ((obj if isinstance(obj, dict) else {}).get("commit") or {}).get("tree") or {}
        sha = tree.get("sha")
        if not isinstance(sha, str) or not sha:
            raise TransportError()
        return sha
