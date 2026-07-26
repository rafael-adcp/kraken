"""The claim-ref CAS: the generation ladder that decides who owns a task.

Part of the kraken protocol package; see __init__.py."""
from __future__ import annotations

import json
from typing import Iterable, Sequence

from .contract import (
    CommitMeta, EXIT_LOST, EXIT_TRANSPORT, Gen, Issue, Json, LEGACY_CLAIM_GEN,
    Sha, Worker
)
from .comments import make_marker, parse_marker
from .transport import Api
from .lease import Lease, NO_LEASE, UNREADABLE_LEASE, parse_iso

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
# Nothing is ever deleted to make room, which is what removes the last window in
# the design: a thief and the live holder renewing race on the identical ref, the
# server picks one, and the loser is told. Deleting a superseded generation is
# garbage collection — losing that delete costs a stray ref, never a lease.
#
# Refs are UI-invisible, which is the whole reason the in-progress label exists:
# it is written after the CAS so a human scanning the issue list can see the ref
# they cannot. Nothing reads it back (§3).

CLAIM_REF_PREFIX = "refs/kraken/claims/"
# git's well-known empty-tree object, present in every repo, so an orphan commit
# needs no prior read; create_claim_commit falls back to HEAD's tree if a host
# rejects it.
EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def claim_ref(issue: Issue, gen: Gen) -> str:
    if gen == LEGACY_CLAIM_GEN:
        return f"{CLAIM_REF_PREFIX}{issue}"
    return f"{CLAIM_REF_PREFIX}{issue}/{gen}"


def parse_claim_ref(ref: str) -> tuple[Issue, Gen] | None:
    """`(issue, generation)` for a claim ref name, or None for anything else.

    Both shapes are accepted: `…/claims/12` is the protocol/5 ref, read as
    generation 0, and `…/claims/12/3` is generation 3. Strict parsing matters
    because GitHub's matching-refs is a plain PREFIX match — asking for
    `kraken/claims/12` also returns `kraken/claims/120/1` — so the caller filters
    on what this returns, never on the prefix it asked for."""
    if not ref.startswith(CLAIM_REF_PREFIX):
        return None
    parts = ref[len(CLAIM_REF_PREFIX):].split("/")
    if len(parts) == 1 and parts[0].isdigit():
        return (int(parts[0]), LEGACY_CLAIM_GEN)
    if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
        return (int(parts[0]), int(parts[1]))
    return None


def _head_tree_sha(api: Api) -> Sha | None:
    """The default branch's tree SHA — the fallback tree for hosts that reject
    the well-known empty-tree object. None on transport failure."""
    obj = api.json("GET", f"/repos/{api.repo}/commits/HEAD")
    if obj is None:
        return None
    tree = (obj.get("commit") or {}).get("tree") or {}
    sha = tree.get("sha")
    return sha if isinstance(sha, str) and sha else None


def create_claim_commit(api: Api, payload: Json) -> Sha | None:
    """Create the orphan commit a claim ref points at: empty tree, no parents,
    message = the kraken marker for `payload`. The server stamps the date, so the
    liveness clock is server-side. Returns the SHA, or None on transport failure."""
    for tree in (EMPTY_TREE_SHA, None):
        if tree is None:
            tree = _head_tree_sha(api)
            if tree is None:
                return None
        status, text = api.request(
            "POST", f"/repos/{api.repo}/git/commits",
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


def claim_ref_create(api: Api, issue: Issue, gen: Gen, sha: Sha) -> str:
    """The CAS itself: create generation `gen` of issue `issue`'s claim ref.
    Returns "won" (created — this worker is now the holder), "lost" (HTTP 422:
    somebody else created this generation first), or "fail" (transport — state
    unknown). The verdict is the integer status code, nothing else.

    This one call arbitrates every contended operation: a first claim (gen 1), a
    steal (holder's gen + 1) and a renewal (own gen + 1) all compete here, so a
    thief and the holder it is stealing from race on the SAME ref name and the
    server picks exactly one."""
    status, _text = api.request(
        "POST", f"/repos/{api.repo}/git/refs",
        {"ref": claim_ref(issue, gen), "sha": sha},
    )
    if 200 <= status < 300:
        return "won"
    if status == 422:
        return "lost"
    return "fail"


def claim_ref_delete(api: Api, issue: Issue, gen: Gen) -> bool:
    """Delete one generation of a claim ref. An already-missing ref (HTTP 422)
    counts as success: it is gone either way, and the delete stays idempotent
    under retries."""
    status, _text = api.request(
        "DELETE", f"/repos/{api.repo}/git/{claim_ref(issue, gen)}"
    )
    return 200 <= status < 300 or status == 422


def drop_generations(api: Api, issue: Issue, gens: Iterable[Gen]) -> bool:
    """Delete a set of generations — how a lease is released (every generation
    of it) and how a superseded one is collected after an advance. True only if
    all of them went; a leftover generation is untidy, never a held lease,
    because the holder is decided by the HIGHEST generation."""
    ok = True
    for gen in gens:
        if not claim_ref_delete(api, issue, gen):
            ok = False
    return ok


def _parse_ref_items(items: Iterable[Json]) -> list[tuple[Issue, Gen, Sha]]:
    """[(issue, gen, sha)] out of a matching-refs payload, dropping anything
    that is not a claim ref — matching-refs is a prefix match, so the filter is
    on the parsed name, never on the prefix that was asked for."""
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        sha = (item.get("object") or {}).get("sha") or ""
        parsed = parse_claim_ref(item.get("ref") or "")
        if sha and parsed is not None:
            out.append((parsed[0], parsed[1], sha))
    return out


def claim_ref_list(api: Api) -> dict[Issue, list[tuple[Gen, Sha]]] | None:
    """Every live claim ref as {issue_number: [(generation, sha), …]}, in one
    paginated matching-refs read. Returns a dict (empty when none), or None on
    transport failure. Generations are not collapsed here: the holder is the
    highest one, and the rest are the superseded refs a reader may collect."""
    items = api.paginated(f"/repos/{api.repo}/git/matching-refs/kraken/claims/")
    if items is None:
        return None
    refs = {}
    for issue, gen, sha in _parse_ref_items(items):
        refs.setdefault(issue, []).append((gen, sha))
    return refs


def claim_refs_of(
    api: Api, issue: Issue,
) -> tuple[bool, list[tuple[Gen, Sha]]]:
    """One issue's claim refs as `(ok, [(generation, sha), …])` — the sorted
    generation ladder, empty when the task is unclaimed. `ok` is False on a
    transport failure, and only then, so 'nobody holds it' is never confused
    with 'the read did not land'."""
    if not str(issue).lstrip("-").isdigit():
        return (False, [])
    items = api.paginated(
        f"/repos/{api.repo}/git/matching-refs/kraken/claims/{int(issue)}"
    )
    if items is None:
        return (False, [])
    return (True, sorted((gen, sha) for i, gen, sha in _parse_ref_items(items)
                         if i == int(issue)))


def resolve_commit_meta(api: Api, shas: Sequence[Sha]) -> CommitMeta | None:
    """Resolve each claim commit's {committedDate, message} in one batched
    GraphQL call (one aliased `object(oid:)` field per distinct SHA), never one
    call per ref — the resolve_depends_on pattern. Returns
    {sha: {"committedDate": ..., "message": ...}}, or None on transport
    failure."""
    if not shas:
        return {}
    owner, name = api.repo.split("/", 1)
    ordered = sorted(set(shas))
    fields = " ".join(
        f'c{i}: object(oid: "{sha}") {{ ... on Commit {{ committedDate message }} }}'
        for i, sha in enumerate(ordered)
    )
    resp = api.graphql(f'{{ repository(owner: "{owner}", name: "{name}") {{ {fields} }} }}')
    if resp is None:
        return None
    repo_obj = resp["data"]["repository"]
    meta = {}
    for i, sha in enumerate(ordered):
        obj = repo_obj.get(f"c{i}") or {}
        meta[sha] = {
            "committedDate": obj.get("committedDate") or "",
            "message": obj.get("message") or "",
        }
    return meta


def claim_ref_head(api: Api, issue: Issue) -> Lease:
    """The head of one issue's lease — the read every ownership question goes
    through: which generation holds it, whose it is, and since when.

    The three outcomes are kept apart on purpose, because they demand different
    answers, and all three are a `Lease`:
      - a present lease — the highest generation, plus `gens`, every generation
        present (so a caller that is releasing knows what to delete and one that
        is advancing knows what to collect). `age`/`live` are left undecided:
        this read knows no TTL, and ownership is what its callers ask about;
      - `NO_LEASE` — no claim ref at all, the task is unclaimed;
      - `UNREADABLE_LEASE` — the read did not land, so the answer is *unknown*
        and no caller may write on the strength of it.
    `epoch` is None when the commit carries no readable date — the same
    fail-open the queue-wide read applies: nothing proves the holder alive."""
    ok, refs = claim_refs_of(api, issue)
    if not ok:
        return UNREADABLE_LEASE
    if not refs:
        return NO_LEASE
    gen, sha = refs[-1]  # sorted: the highest generation is the holder
    meta = resolve_commit_meta(api, [sha])
    if meta is None:
        return UNREADABLE_LEASE
    entry = meta.get(sha) or {}
    payload = parse_marker(entry.get("message") or "") or {}
    return Lease(
        gen=gen,
        sha=sha,
        worker=payload.get("worker") or None,
        epoch=parse_iso(entry.get("committedDate") or ""),
        gens=tuple(g for g, _s in refs),
    )


def advance_lease(
    api: Api, issue: Issue, gen: Gen, payload: Json,
) -> tuple[str, Gen | None, Sha | None]:
    """Take (or keep) the lease by creating the generation ABOVE `gen` — the one
    contended write, shared by the steal and the renewal (PROTOCOL.md §5.2).

    Returns `(verdict, gen, sha)`: "won" with the new generation and its commit,
    "lost" when another worker created that generation first, or "fail-commit" /
    "fail-ref" on transport — the two failures stay apart because a caller's
    diagnostic names the stage that actually broke. Nothing is deleted here, so
    there is no instant at which the task is unheld and no way for two workers to
    both believe they took it: they are competing to create one identical ref
    name, and the server admits one."""
    sha = create_claim_commit(api, payload)
    if sha is None:
        return ("fail-commit", None, None)
    verdict = claim_ref_create(api, issue, gen + 1, sha)
    if verdict == "fail":
        return ("fail-ref", None, None)
    return (verdict, gen + 1 if verdict == "won" else None,
            sha if verdict == "won" else None)


def claim_ref_owner(api: Api, issue: Issue) -> Worker | None:
    """The worker named in the claim commit the ref for `issue` currently points
    at, or None when the ref is absent or unreadable. This is how a lost CAS
    (HTTP 422) is told apart: a 422 is a genuine loss only when the ref belongs
    to a DIFFERENT worker; a worker re-claiming its OWN in-flight claim after a
    network failure already owns the task (PROTOCOL.md §5's re-check caveat).
    None (transport/absent) is treated by the caller as 'not mine', so an
    ambiguous read never turns a real loss into a false win."""
    return claim_ref_head(api, issue).worker


def hold_lease(
    command: str, api: Api, issue: Issue, worker: Worker,
) -> tuple[int | None, Lease]:
    """The write-after-expiry check (PROTOCOL.md §5.3): prove this worker still
    holds the lease BEFORE any write of a transition. Returns `(code, head)` —
    `code` None when the lease is ours and the caller may proceed (with `head`
    naming the generations to release), otherwise the exit code to return.

    Ownership is the HIGHEST generation: a thief takes the lease by creating the
    one above ours, so our own ref still existing proves nothing — what proves it
    is that nobody has climbed past us.

    This is what makes a short TTL safe. A worker that stalled long enough to be
    stolen from — a suspended laptop, a rate-limit wait, a very long build — is
    otherwise indistinguishable from a live one until it tries to deliver, and a
    delivery written onto a task somebody else is now executing is worse than no
    delivery at all. So: not ours, or gone, means write NOTHING and exit 10; an
    ambiguous read means exit 20 and re-check, never a write on a guess.

    A lease that is ours but past its TTL still passes: it expired, but nobody
    took it, and finishing the transition frees it honestly — the check is about
    who holds the lease, not how old it is."""
    head = claim_ref_head(api, issue)
    if head.unknown:
        print(f"{command}: gh-failure issue={issue} stage=lease")
        return (EXIT_TRANSPORT, head)
    if not head.present:
        print(f"{command}: lost-lease issue={issue} — the lease is gone, "
              "re-claim the task before writing to it")
        return (EXIT_LOST, head)
    if not head.held_by(worker):
        holder = head.worker or "another worker"
        print(f"{command}: lost-lease issue={issue} — the lease is held by {holder}")
        return (EXIT_LOST, head)
    return (None, head)
