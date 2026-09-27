"""The claim-ref CAS: the generation ladder that decides who owns a task.

Part of the kraken protocol package; see __init__.py."""
from __future__ import annotations

import json
from typing import Iterable, Sequence

from .contract import (
    CommitMeta, EXIT_LOST, EXIT_TRANSPORT, Gen, Issue, Json, LEGACY_CLAIM_GEN,
    Sha, Worker
)
from .comments import make_marker
from .transport import Api
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


def kraken_ref_items(api: Api) -> list[Json] | None:
    """Every ref under `refs/kraken/`, undecoded, or None on transport failure.
    `Refs.all` and `States.all` each parse their own family out of it."""
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


class Refs:
    """One repo's claim-ref ladder, the CAS that arbitrates it, and the lease it
    carries (PROTOCOL.md §4/§5). Cheap to construct where needed."""

    def __init__(self, api: Api):
        self.api = api

    # --- reading the ladder ---------------------------------------------------

    def all(self, items: Iterable[Json] | None = None,
            ) -> dict[Issue, list[tuple[Gen, Sha]]] | None:
        """Every claim ref as {issue: [(generation, sha), …]}, uncollapsed, or
        None on transport failure. `items` reuses an already-fetched payload."""
        if items is None:
            items = kraken_ref_items(self.api)
            if items is None:
                return None
        refs = {}
        for issue, gen, sha in _parse_ref_items(items):
            refs.setdefault(issue, []).append((gen, sha))
        return refs

    def of(self, issue: Issue) -> tuple[bool, list[tuple[Gen, Sha]]]:
        """`(ok, sorted ladder)` for one issue; `ok` is False only on transport
        failure, so "unclaimed" is never confused with "unread"."""
        if not str(issue).lstrip("-").isdigit():
            return (False, [])
        items = self.api.paginated(
            f"/repos/{self.api.repo}/git/matching-refs/kraken/claims/{int(issue)}"
        )
        if items is None:
            return (False, [])
        return (True, sorted((gen, sha) for i, gen, sha in _parse_ref_items(items)
                             if i == int(issue)))

    def commit_meta(self, shas: Sequence[Sha]) -> CommitMeta | None:
        """{sha: {committedDate, message}} in one aliased fan-out, or None on
        transport failure. Aliases are indexes (`c0`…) because a GraphQL alias
        must be a name and a SHA may start with a digit."""
        ordered = sorted(set(shas))
        fields = [
            f'c{i}: object(oid: "{sha}") {{ ... on Commit {{ committedDate message }} }}'
            for i, sha in enumerate(ordered)
        ]
        repo_obj = self.api.aliased(fields)
        if repo_obj is None:
            return None
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
        ok, refs = self.of(issue)
        if not ok:
            return UNREADABLE_LEASE
        if not refs:
            return NO_LEASE
        meta = self.commit_meta([max(refs)[1]])
        if meta is None:
            return UNREADABLE_LEASE
        return Lease.from_ladder(refs, meta)

    def owner(self, issue: Issue) -> Worker | None:
        """The worker holding `issue`, or None when absent or unreadable."""
        return self.head(issue).worker

    # --- writing the ladder ---------------------------------------------------

    def commit(self, payload: Json) -> Sha | None:
        """The orphan commit a ref points at: empty tree, no parents, the marker
        as message. The server stamps the date. None on transport failure."""
        for tree in (EMPTY_TREE_SHA, None):
            if tree is None:
                tree = self._head_tree_sha()
                if tree is None:
                    return None
            status, text = self.api.request(
                "POST", f"/repos/{self.api.repo}/git/commits",
                {"message": make_marker(payload), "tree": tree, "parents": []},
            )
            if 200 <= status < 300:
                try:
                    sha = json.loads(text).get("sha")
                except (ValueError, json.JSONDecodeError):
                    return None
                return sha if isinstance(sha, str) and sha else None
            if status != 422:
                return None
            # 422 on the empty tree: this host wants a reachable tree — fall back.
        return None

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

    def advance(self, issue: Issue, gen: Gen,
                payload: Json) -> tuple[str, Gen | None, Sha | None]:
        """Create the generation above `gen` (§5.2): `(verdict, gen, sha)`, the
        verdict being "won", "lost", "fail-commit" or "fail-ref"."""
        sha = self.commit(payload)
        if sha is None:
            return ("fail-commit", None, None)
        verdict = self.create(issue, gen + 1, sha)
        if verdict == "fail":
            return ("fail-ref", None, None)
        return (verdict, gen + 1 if verdict == "won" else None,
                sha if verdict == "won" else None)

    def hold(self, issue: Issue,
             worker: Worker) -> tuple[int | None, Lease, str]:
        """The write-after-expiry check (§5.3): prove this worker still holds
        the lease before a transition writes anything. `(code, head, reason)`,
        `code` None when the caller may proceed.

        This is what makes a short TTL safe: a worker that stalled long enough
        to be stolen from writes nothing. Ours-but-expired still passes — nobody
        took it, and the question is who holds the lease, not its age."""
        head = self.head(issue)
        if head.unknown:
            return (EXIT_TRANSPORT, head, f"gh-failure issue={issue} stage=lease")
        if not head.present:
            return (EXIT_LOST, head,
                    f"lost-lease issue={issue} — the lease is gone, "
                    "re-claim the task before writing to it")
        if not head.held_by(worker):
            holder = head.worker or "another worker"
            return (EXIT_LOST, head,
                    f"lost-lease issue={issue} — the lease is held by {holder}")
        return (None, head, "")

    # --- internals ------------------------------------------------------------

    def _head_tree_sha(self) -> Sha | None:
        """HEAD's tree SHA, for hosts that reject the empty tree."""
        obj = self.api.json("GET", f"/repos/{self.api.repo}/commits/HEAD")
        if obj is None:
            return None
        tree = (obj.get("commit") or {}).get("tree") or {}
        sha = tree.get("sha")
        return sha if isinstance(sha, str) and sha else None
