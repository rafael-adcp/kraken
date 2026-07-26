"""Reading the queue: the batched walk, comment hydration, the requeue
derivation, and the startable filter.

Part of the kraken protocol package; see __init__.py."""
from __future__ import annotations

import argparse
import re
import time
from typing import Iterable, Sequence

from .contract import (
    CommitMeta, EXIT_OK, EXIT_TRANSPORT, Epoch, HELD_LABELS, Issue, Json,
    Node, PRIORITY_LABEL, Sha, Worker
)
from .comments import DISCLAIMER, parse_marker
from .transport import Api
from .lease import (
    Lease, holder_shas, lease_state, lease_ttl_seconds, live_leases
)
from .refs import Refs

# --- subcommand: list-startable ---------------------------------------------
#
# Queue fetch and blocked-by check are batched through GraphQL, so an idle poll
# costs a queue-size-independent number of round trips. GraphQL's
# `issues(labels: [...])` is a UNION (unlike REST's AND), so we filter server-side
# on the single "kraken-task" label and match the project label client-side.

# How many trailing comments a HYDRATED task carries. The requeue derivation (§6)
# only ever needs the NEWEST comment of two classes — the last worker comment and
# any operator reply after it — so a window cannot mislead it: a window holding no
# worker comment means the last one is older than the window, which makes every
# operator comment inside it newer, the correct verdict. 25 is therefore about
# payload, not correctness.
QUEUE_COMMENT_WINDOW = 25

# How many issues one hydration call asks about. The window is per issue, so the
# chunk bounds the node count of a single query rather than the number of queries
# — 50 × 25 keeps a hydration page about the size of a queue page.
COMMENT_HYDRATE_CHUNK = 50








# --- the requeue derivation (PROTOCOL.md §6) ---------------------------------
# When the operator answers a held task, the task rejoins the queue. Through
# protocol/4 that was a MUTATION: a workflow watched the comment stream and
# removed the holding label. But "an operator replied after the last worker
# comment" is a property of the thread, readable at any time — so protocol/5
# DERIVES it on the read instead. No workflow, no label write, and no window
# where the queue disagrees with the thread.
#
# The derivation reads a trailing comment window (QUEUE_COMMENT_WINDOW). It used
# to ride the queue walk, which made it free per task but charged every task for
# it; it is now hydrated for the held/leased handful instead (see
# hydrate_comment_windows), which is the same free on an idle queue and O(held)
# rather than O(queue) on a busy one.

# The labels a reply can lift are exactly the labels that hold (HELD_LABELS):
# both are operator-facing states, and answering one is what ends it. Under
# protocol/6 this was a second, narrower tuple because `in-progress` held without
# being requeueable; protocol/7 made that label write-only, so the two sets
# coincide and one name is enough.


def is_worker_comment(body: str) -> bool:
    """Whether a comment was posted by a worker — the requeue derivation's
    discriminator (PROTOCOL.md §4). **Structural**: a worker comment is one that
    carries a hidden kraken marker on ANY line. Every worker-posted comment wears
    one (the transition markers for state changes, the `note` marker for a
    free-form note), so marker presence — not the presentation text — is the
    arbiter, closing the fragility class kraken-protocol removed everywhere else.
    It also subsumes the coordination repo's own bot comments (the validator's
    carry a `validation` marker), which is why the derivation needs no author
    metadata in the query at all.

    A comment whose FIRST line opens with the attribution disclaimer also counts,
    as a back-compat fallback for legacy threads and any comment written before
    `note` gained its marker (the prefix is derived from the DISCLAIMER constant,
    never a second copy). The disclaimer stays the human-facing attribution but is
    no longer load-bearing for requeue.

    Accepted edge (§4): an operator who pastes a raw kraken marker into a reply is
    read as a worker and will not requeue — the same class of edge as an operator
    quoting the disclaimer on the first line; removing the held label by hand is
    the escape hatch."""
    if any(parse_marker(line) is not None for line in body.split("\n")):
        return True
    prefix = DISCLAIMER.split("{worker}")[0]  # "> 🐙 **Kraken worker `"
    first_line = body.split("\n", 1)[0].rstrip("\r")
    return first_line.startswith(prefix)


def has_requeue_directive(body: str) -> bool:
    """Whether a comment carries an EXPLICIT, STRUCTURED requeue directive — the
    only thing that bounces a DELIVERED (awaiting-merge) task back for rework, so
    a prose sentence merely starting a line with "requeue:" no longer bounces a
    ready branch by accident. Two accepted forms: a protocol/3
    `<!-- kraken {"type":"requeue"} -->` marker, or a standalone directive line
    whose only content is `requeue`/`requeue:` (case-insensitive)."""
    lines = body.split("\n")
    for raw in lines:
        marker = parse_marker(raw)
        if marker and marker.get("type") == "requeue":
            return True
    for raw in lines:
        if re.match(r"^\s*requeue:?\s*$", raw, re.IGNORECASE):
            return True
    return False


def comment_nodes_of(node: Node) -> list[Json]:
    """A task node's trailing comment window, oldest-first — empty for a node the
    hydration pass did not fill.

    That empty answer is not a guess. `hydrate_comment_windows` fills exactly the
    nodes the two comment readers can be called on — `requeued_labels` only reads
    a node wearing a held label with no live lease, `expiry_count` only a node
    whose lease expired — so an unhydrated node is one neither reader visits, and
    the emptiness is unobservable rather than merely harmless."""
    return (node.get("comments") or {}).get("nodes") or []


def comment_hungry(
    nodes: Sequence[Node], leases: dict[Issue, Lease],
) -> set[Issue]:
    """The issue numbers whose comment window a read of this queue actually needs
    — the set `hydrate_comment_windows` fetches, as a pure function so the
    fetch's scope is testable without transport.

    Exactly two readers consume comments, and each names its own tasks:

      1. the requeue derivation (§6) — a task wearing a HELD label. A live lease
         outranks the thread (`requeued_labels` returns early), so a locked task
         is not hungry however it is labelled;
      2. `expiry_count` (§6 rule 2) — a task whose lease EXPIRED, which the
         reconciler counts steals on before escalating. A live lease is not
         reconciled at all, and a ref on an issue outside the walk is an orphan
         lock deleted without ever reading a comment.

    Everything else in the queue — the startable tasks, which is nearly all of it
    — is decided from labels, blocked-by and body alone."""
    live = live_leases(leases)
    held_labels = set(HELD_LABELS)
    hungry = set()
    for node in nodes:
        number = node["number"]
        if number in live:
            continue
        if label_names_of(node) & held_labels:
            hungry.add(number)
    walked = {node["number"] for node in nodes}
    for number, lease in leases.items():
        if lease.expired and number in walked:
            hungry.add(number)
    return hungry




def requeue_verdict(held: str, comments: Sequence[Json]) -> bool:
    """Whether an operator reply has requeued a task held by `held` labels — a
    PURE read of the thread (PROTOCOL.md §6), so it is decided identically by
    every reader and needs no write to become true.

    The rule: an operator comment (one carrying no kraken marker) newer than the
    newest worker comment. The two held states stay asymmetric — a bare reply
    requeues `needs-decision` (a human comment is almost always the answer), but
    `awaiting-merge` is already *delivered* and stays held unless a reply carries
    an explicit requeue directive, so a `requeue:` buried in prose cannot bounce a
    ready branch.

    Ordering is the comment list's own (GitHub returns a thread oldest-first);
    nothing here reads a timestamp, so a re-posted comment cannot reorder the
    verdict."""
    worker_at = -1
    for i, rec in enumerate(comments):
        if is_worker_comment(rec.get("body") or ""):
            worker_at = i
    replies = [rec for i, rec in enumerate(comments)
               if i > worker_at and not is_worker_comment(rec.get("body") or "")]
    if not replies:
        return False
    if "awaiting-merge" in held:
        return any(has_requeue_directive(rec.get("body") or "") for rec in replies)
    return True


def requeued_labels(node: Node, live: dict[Issue, Sha]) -> tuple[str, ...]:
    """The held labels this task carries that an operator reply has already
    lifted — a tuple, empty when the task is genuinely held. Pure, so the
    classification and the claim that follows it reach the same verdict from the
    same node without a second read.

    A **live lease** outranks any reading of the timeline: the lock is the truth
    (§5), and a comment on a claimed task is context for its worker, not a
    requeue. An EXPIRED lease outranks nothing — it holds no task (§6), so
    `live` is the live-lease view, never the raw ref list. The lease is the ONLY
    thing that outranks the thread here: `in-progress` is write-only (§3), so a
    stale badge left on a task cannot suppress a requeue the operator earned."""
    if node["number"] in live:
        return ()
    labels = set(label_names_of(node))
    held = tuple(h for h in HELD_LABELS if h in labels)
    if not held:
        return ()
    return held if requeue_verdict(held, comment_nodes_of(node)) else ()






def cmd_list_startable(args: argparse.Namespace) -> int:
    rows = Queue(args.api).candidates(args.project)
    if rows is None:
        return EXIT_TRANSPORT

    if args.snapshot:
        for number, _, _, state in sorted(rows, key=lambda r: r[0]):
            print(f"{number}:{state}")
    else:
        for number, title, _, state in rows:  # priority-first, then createdAt FIFO
            if state == "startable":
                print(f"{number}\t{title}")
    return EXIT_OK


def project_names_of(node: Node) -> list[str]:
    """The project:<name> suffixes carried by a queue node's labels."""
    names = set()
    for lbl in node.get("labels", {}).get("nodes", []):
        name = lbl.get("name", "")
        if name.startswith("project:"):
            names.add(name[len("project:"):])
    return names


def label_names_of(node: Node) -> set[str]:
    return {lbl.get("name", "") for lbl in node.get("labels", {}).get("nodes", [])}


def claim_meta_of(
    sha: Sha, commit_meta: CommitMeta,
) -> tuple[Worker | None, str | None, str | None]:
    """Decode one claim ref's commit into (worker, msg, anchor_iso) — the
    marker payload plus the server-stamped committedDate. This is the ONE
    liveness read `status` and the reaper share: the ref's commit date is the
    staleness clock, so nothing on the issue timeline (an operator poking a
    dead worker's thread, a bot comment) can ever make a claim look alive.
    Unreadable pieces come back as None, never guessed."""
    commit = commit_meta.get(sha) or {}
    payload = parse_marker(commit.get("message") or "") or {}
    worker = payload.get("worker") or None
    msg = payload.get("msg") or None
    anchor = commit.get("committedDate") or None
    return worker, msg, anchor
NO_RESPONSE_PLACEHOLDER = "_No response_"


def section_body(body: str, heading: str) -> str:
    """The trimmed content under `### HEADING` up to the next `### ` heading (or
    EOF). A hand-written issue lacking the heading yields nothing; an issue-form
    field left blank renders as the literal `_No response_`. Mirrors the awk the
    validate-task workflow used to carry."""
    grab = False
    out = []
    target = "### " + heading
    for raw in body.split("\n"):
        line = raw.rstrip("\r")
        if line == target:
            grab = True
            continue
        if grab and line.startswith("### "):
            grab = False
        if grab:
            out.append(line)
    return "\n".join(out)


def is_empty_section(content: str) -> bool:
    """True when a section's content is blank or only the issue-form
    `_No response_` placeholder — each line trimmed, blank lines dropped."""
    nonblank = [ln.strip() for ln in content.split("\n") if ln.strip() != ""]
    joined = "\n".join(nonblank)
    return joined == "" or joined == NO_RESPONSE_PLACEHOLDER

class Queue:
    """The coordination repo's task queue as this program reads it: the batched
    walk, the lease state that comes with it, comment hydration, and the
    startable filter.

    Six functions took `api` first and are the two halves of one job — reading
    the queue and deciding which of it is startable. `acquire_next` had to inject
    two of them separately to stay testable; one collaborator replaces both.

    The DECISIONS made on what is read — the requeue derivation, the hungry set,
    the section parsing — stay module-level functions: they are pure, they touch
    no transport, and they are tested as such."""

    def __init__(self, api: Api):
        self.api = api

    def open_tasks(self) -> list[Node] | None:
        """Every OPEN kraken-task issue in the repo, across all projects — number,
        title, createdAt, body, labels and native blocked-by — in one paginated
        GraphQL walk. Returns the node list, or None on transport failure.

        Deliberately does NOT carry comments. This walk is the hot read: every
        worker's watcher runs it once a minute, so a trailing comment window on every
        node was the queue's first scaling wall — a 200-task queue shipped 5,000
        comment bodies per poll, per worker, to answer a question about a handful of
        them. Both readers that need comments (the requeue derivation and
        `expiry_count`) only ever ask about tasks that are HELD or LEASED, so the
        window is fetched for exactly those — see `hydrate_comment_windows`."""
        owner, name = self.api.repo.split("/", 1)
        nodes = []
        cursor = None
        while True:
            after = f', after: "{cursor}"' if cursor else ""
            query = (
                f'{{ repository(owner: "{owner}", name: "{name}") {{ '
                f'issues(states: OPEN, labels: ["kraken-task"], first: 100{after}) {{ '
                f'pageInfo {{ hasNextPage endCursor }} '
                f'nodes {{ number title createdAt body '
                f'labels(first: 20) {{ nodes {{ name }} }} '
                f'blockedBy(first: 50) {{ nodes {{ number state }} }} }} }} }} }}'
            )
            resp = self.api.graphql(query)
            if resp is None:
                return None
            page = resp["data"]["repository"]["issues"]
            nodes.extend(page["nodes"])
            if not page["pageInfo"]["hasNextPage"]:
                return nodes
            cursor = page["pageInfo"]["endCursor"]

    def read(self, now: Epoch | None = None, ttl: int | None = None,
             ) -> tuple[list[Node], dict[Issue, Lease]] | None:
        """One queue read: every open kraken-task node (repo-wide, BEFORE any project
        filter) plus the LEASE state of every claim ref. The single fetch the
        reconciler (§6), the startable classification and the claim path all consume,
        so a drain that does all three pays for it once. Returns (nodes, leases), or
        None on transport failure.

        Resolving the lease costs one extra batched call — and only when a ref
        actually exists: `resolve_commit_meta` answers `{}` for an empty ref list
        without a request, so an idle queue reads exactly as cheaply as it did under
        protocol/5, and a busy one pays O(1) in the number of claims, never O(N).

        The comment hydration obeys the same rule and for the same reason: the leases
        have to be known before the hungry set can be (a live lease outranks the
        thread), so it runs last, and it asks for nothing when nothing is held."""
        nodes = self.open_tasks()
        if nodes is None:
            return None
        claim_refs = Refs(self.api).all()
        if claim_refs is None:
            return None
        commit_meta = Refs(self.api).commit_meta(holder_shas(claim_refs))
        if commit_meta is None:
            return None
        leases = lease_state(claim_refs, commit_meta,
                             time.time() if now is None else now,
                             lease_ttl_seconds(ttl))
        if self.hydrate(nodes, leases) is None:
            return None
        return (nodes, leases)

    def candidates(self, project: str, include_body: bool = False,
                   read: tuple[list[Node], dict[Issue, Lease]] | None = None,
                   ) -> list[tuple] | None:
        """The shared startable/held classification list-startable and watch's
        snapshot both read — one code path so the filter can't drift between them.
        Returns a list of (number, title, createdAt, "startable"|"held") ordered
        priority:high-first then oldest-first by createdAt within each tier (a stable
        sort — see PRIORITY_LABEL), or None on transport failure. With
        include_body=True each row
        gains a fifth element, the issue body, so claim-next can brief a subagent
        from the win without a second fetch (the GraphQL walk already has it).

        `read` accepts an already-fetched (nodes, leases) pair from `read`,
        which is how claim-next classifies the state its reconcile pass just
        produced without re-fetching it.

        Held means: a held label (needs-decision / awaiting-merge — an operator-facing
        state) OR a **live lease** (the lock). Those are the only two kinds of hold
        there are, and they do not overlap: `in-progress` is write-only (§3), so the
        lease answers for it and a task wearing a stale badge with no live lease is
        offered like any other. An EXPIRED lease holds nothing either (§5): the task
        is offered, and the claim that follows steals the lease.

        A held LABEL is not the last word, though: a task whose operator has already
        replied is requeued by derivation (§6, requeued_labels) and rejoins the
        candidates — it still has to clear its dependencies like any other."""
        if read is None:
            read = self.read()
            if read is None:
                return None
        nodes, leases = read
        live = live_leases(leases)
        project_label = f"project:{project}"
        nodes = [
            n for n in nodes
            if project_label in {l.get("name", "") for l in n.get("labels", {}).get("nodes", [])}
        ]
        # priority:high tasks lead; createdAt breaks ties FIFO within each tier. The
        # key sorts on (not-high, createdAt) so the boolean puts the high tier first
        # and the timestamp keeps the older task ahead inside a tier — a scheduling
        # preference layered on top of pure FIFO (see PRIORITY_LABEL).
        def _is_high(node):
            return PRIORITY_LABEL in {
                l.get("name", "") for l in node.get("labels", {}).get("nodes", [])
            }
        nodes.sort(key=lambda n: (not _is_high(n), n.get("createdAt", "")))

        rows = []            # [number, title, createdAt, state-or-None, body]
        fallback_targets = []  # (row_index, dep_number) needing the depends-on batch

        for node in nodes:
            number = node["number"]
            title = node.get("title", "")
            created = node.get("createdAt", "")
            body = node.get("body") or ""
            label_names = [l.get("name", "") for l in node.get("labels", {}).get("nodes", [])]
            # A held label only holds while the operator has not answered: the
            # requeue derivation lifts the ones an operator reply already cleared.
            still_held = set(HELD_LABELS) - set(requeued_labels(node, live))
            if any(h in label_names for h in still_held) or number in live:
                rows.append([number, title, created, "held", body])
                continue

            blockers = node.get("blockedBy", {}).get("nodes", [])
            if blockers:
                blocked = any(str(b.get("state", "")).upper() == "OPEN" for b in blockers)
                rows.append([number, title, created, "held" if blocked else "startable", body])
                continue

            dep = None
            for line in body.split("\n"):
                m = re.match(r"^depends-on: *#([0-9]+)", line)
                if m:
                    dep = int(m.group(1))
                    break
            if dep is None:
                rows.append([number, title, created, "startable", body])
                continue
            rows.append([number, title, created, None, body])
            fallback_targets.append((len(rows) - 1, dep))

        if fallback_targets:
            dep_open = self._depends_on(sorted({dep for _, dep in fallback_targets}))
            if dep_open is None:
                return None
            for idx, dep in fallback_targets:
                rows[idx][3] = "held" if dep_open.get(dep, False) else "startable"

        if include_body:
            return [tuple(r) for r in rows]
        return [(n, t, c, s) for n, t, c, s, _ in rows]

    def hydrate(self, nodes: list[Node],
                leases: dict[Issue, Lease]) -> list[Node] | None:
        """Attach the trailing comment window to the queue nodes that need one, in
        place, and answer the nodes back — or None on transport failure, which the
        callers propagate exactly like a failed walk.

        This is the second half of the split that took the comment window off the hot
        read (see fetch_open_tasks): the walk answers "what is in the queue" for every
        task, and this answers "what does the thread say" for the few whose answer
        depends on it. An idle queue hydrates nothing and pays nothing."""
        hungry = comment_hungry(nodes, leases)
        windows = self.comment_windows(hungry)
        if windows is None:
            return None
        for node in nodes:
            window = windows.get(node["number"])
            if window is not None:
                node["comments"] = {"nodes": window}
        return nodes

    def comment_windows(self,
                        numbers: Iterable[Issue]) -> dict[Issue, list[Json]] | None:
        """The trailing comment window of the named issues, `{number: [nodes]}`, in
        batched aliased GraphQL calls (`iN: issue(number: N) { comments(last: …) }`)
        — the same shape `resolve_depends_on` uses, for the same reason: a fan-out
        the queue walk no longer pays for on every task must not become one request
        per task either. Returns None on transport failure.

        Cost is O(len(numbers) / COMMENT_HYDRATE_CHUNK) calls, and O(1) — no call at
        all — for the overwhelmingly common empty ask."""
        if not numbers:
            return {}
        owner, name = self.api.repo.split("/", 1)
        numbers = sorted(numbers)
        out = {}
        for start in range(0, len(numbers), COMMENT_HYDRATE_CHUNK):
            chunk = numbers[start:start + COMMENT_HYDRATE_CHUNK]
            fields = " ".join(
                f"i{n}: issue(number: {n}) {{ comments(last: {QUEUE_COMMENT_WINDOW}) "
                f"{{ nodes {{ body createdAt }} }} }}"
                for n in chunk
            )
            resp = self.api.graphql(f'{{ repository(owner: "{owner}", name: "{name}") {{ {fields} }} }}')
            if resp is None:
                return None
            repo_obj = resp["data"]["repository"]
            for n in chunk:
                obj = repo_obj.get(f"i{n}") or {}
                out[n] = ((obj.get("comments") or {}).get("nodes")) or []
        return out

    def _depends_on(self,
                    targets: Iterable[Issue]) -> dict[Issue, str] | None:
        """Resolve every `depends-on: #N` fallback target's open/closed state in
        one batched GraphQL call (one aliased `iN: issue(number: N) { state }`
        field per distinct target), never one call per candidate. Returns
        {number: is_open}, or None on transport failure."""
        if not targets:
            return {}
        owner, name = self.api.repo.split("/", 1)
        fields = " ".join(f"i{n}: issue(number: {n}) {{ state }}" for n in targets)
        resp = self.api.graphql(f'{{ repository(owner: "{owner}", name: "{name}") {{ {fields} }} }}')
        if resp is None:
            return None
        repo_obj = resp["data"]["repository"]
        return {
            n: str((repo_obj.get(f"i{n}") or {}).get("state", "")).upper() == "OPEN"
            for n in targets
        }
