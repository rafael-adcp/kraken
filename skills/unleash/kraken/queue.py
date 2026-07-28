"""Reading the queue: the batched walk, comment hydration, the requeue
derivation, and the startable filter.

Part of the kraken protocol package; see __init__.py."""
from __future__ import annotations

import argparse
import dataclasses
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


def comment_hungry(
    tasks: Sequence[Task], leases: dict[Issue, Lease],
) -> set[Issue]:
    """The issue numbers whose comment window a read of this queue actually needs
    — the set `Queue.hydrate` fetches, as a pure function so the fetch's scope is
    testable without transport.

    Exactly two readers consume comments, and each names its own tasks:

      1. the requeue derivation (§6) — a task wearing a HELD label. A live lease
         outranks the thread (`Task.requeued` returns early), so a locked task
         is not hungry however it is labelled;
      2. `Task.expiries` (§6 rule 2) — a task whose lease EXPIRED, which the
         reconciler counts steals on before escalating. A live lease is not
         reconciled at all, and a ref on an issue outside the walk is an orphan
         lock deleted without ever reading a comment.

    Everything else in the queue — the startable tasks, which is nearly all of it
    — is decided from labels, blocked-by and body alone."""
    live = live_leases(leases)
    held_labels = set(HELD_LABELS)
    hungry = set()
    for task in tasks:
        if task.number in live:
            continue
        if task.labels & held_labels:
            hungry.add(task.number)
    walked = {task.number for task in tasks}
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




# --- the issue-form body -----------------------------------------------------
# Pure string readers, kept free of `Task`: the same two are applied to a body
# read straight off REST — `validate` reads one issue, `task_brief` reads the
# issue a resume verdict fetched — where no queue walk and therefore no `Task`
# exists.

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


# --- one task, as the startable filter sees it -------------------------------

DEPENDS_ON_RE = re.compile(r"^depends-on: *#([0-9]+)", re.MULTILINE)


class Task:
    """One open kraken-task issue, as the queue walk returned it — and the only
    thing above the walk that knows what a GraphQL issue node looks like.

    It was a bare `dict` passed everywhere, with a cluster of module-level
    functions whose single argument was always that same hash: `label_names_of`,
    `project_names_of`, `comment_nodes_of`, `depends_on_ref`, `node_state`,
    `requeued_labels`, `expiry_count`, and the per-task half of `queue_hygiene`.
    Seven readers in four modules, each reaching into the wire format by key.

    The wire shape is decoded ONCE, here, in the constructor. That is what makes
    the mutations honest: `reclaim` edits a label set, and `hydrate` fills a
    comment list — neither rebuilds `{"nodes": [{"name": …}]}` to be read back by
    the next reader, which is what folding a repair into a queue read used to
    cost.

    Mutable on purpose, and only in the two ways a queue read actually mutates a
    task: comments arrive after the walk (`hydrate`), and an applied reconcile is
    folded back in (`reclaim`). Everything else is a query."""

    def __init__(self, node: Node):
        self.number: Issue = node["number"]
        self.title: str = node.get("title", "")
        self.created: str = node.get("createdAt", "")
        self.body: str = node.get("body") or ""
        self.labels: set[str] = {
            lbl.get("name", "")
            for lbl in (node.get("labels") or {}).get("nodes") or []
        }
        # The trailing comment window, oldest-first — empty until `hydrate`
        # fills it, and empty forever for a task neither comment reader visits.
        # That empty answer is not a guess: `Queue.hydrate` fills exactly the
        # tasks `comment_hungry` names, which is exactly the tasks `requeued`
        # and `expiries` can be called on.
        self.comments: list[Json] = list(
            (node.get("comments") or {}).get("nodes") or [])
        self._blockers: list[Json] = list(
            (node.get("blockedBy") or {}).get("nodes") or [])

    def __repr__(self) -> str:
        return f"Task(#{self.number} {self.title!r})"

    # --- what the labels say --------------------------------------------------

    @property
    def projects(self) -> set[str]:
        """The `project:<name>` suffixes this task's labels carry."""
        return {name[len("project:"):] for name in self.labels
                if name.startswith("project:")}

    @property
    def held(self) -> tuple[str, ...]:
        """The HELD labels this task wears, in HELD_LABELS order."""
        return tuple(h for h in HELD_LABELS if h in self.labels)

    # --- what the thread says -------------------------------------------------

    def requeued(self, live: dict[Issue, Sha]) -> tuple[str, ...]:
        """The held labels an operator reply has already lifted — a tuple, empty
        when the task is genuinely held. Pure, so the classification and the
        claim that follows it reach the same verdict without a second read.

        A **live lease** outranks any reading of the timeline: the lock is the
        truth (§5), and a comment on a claimed task is context for its worker,
        not a requeue. An EXPIRED lease outranks nothing — it holds no task (§6),
        so `live` is the live-lease view, never the raw ref list. The lease is
        the ONLY thing that outranks the thread here: `in-progress` is write-only
        (§3), so a stale badge cannot suppress a requeue the operator earned."""
        if self.number in live:
            return ()
        held = self.held
        if not held:
            return ()
        return held if requeue_verdict(held, self.comments) else ()

    @property
    def expiries(self) -> int:
        """How many times this task's lease has already expired and been stolen —
        counted from the `lease-expired` markers each steal leaves (§5). Only
        read for a task whose lease EXPIRED, which is exactly what makes it one
        the hydration pass filled, so the count reads a window already in hand; a
        window that has scrolled past the oldest steals undercounts, which errs
        toward giving the task another worker rather than escalating early."""
        total = 0
        for rec in self.comments:
            for line in (rec.get("body") or "").split("\n"):
                payload = parse_marker(line)
                if payload and payload.get("type") == "lease-expired":
                    total += 1
        return total

    # --- what the body says ---------------------------------------------------

    @property
    def depends_on(self) -> Issue | None:
        """The `depends-on: #N` target declared in the body, or None. This is the
        TEXT fallback: GitHub's native blocked-by link supersedes it and is read
        off the queue walk, so it is only consulted for a task with no link."""
        m = DEPENDS_ON_RE.search(self.body)
        return int(m.group(1)) if m else None

    def section(self, heading: str) -> str:
        """The trimmed content under `### HEADING`, "" when blank or absent."""
        content = section_body(self.body, heading)
        return "" if is_empty_section(content) else content.strip()

    @property
    def missing(self) -> list[str]:
        """What makes this queue entry dead on arrival (PROTOCOL.md §2.1): no
        `project:<name>` label (invisible to every worker), or an empty/absent
        Goal or Acceptance section (a worker claims it, then stalls). Empty for a
        task a worker can actually start."""
        missing = []
        if not self.projects:
            missing.append("project label")
        if not self.section("Goal"):
            missing.append("Goal")
        if not self.section("Acceptance"):
            missing.append("Acceptance")
        return missing

    # --- the startable verdict ------------------------------------------------

    def state(self, live: dict[Issue, Sha]) -> str | None:
        """This task's startable/held verdict against the live-lease view — or
        None when the answer depends on the `depends-on` target's state, which
        the caller resolves for the whole page in one batched call.

        Held means: a held label (needs-decision / awaiting-merge — an
        operator-facing state) OR a **live lease** (the lock). Those are the only
        two kinds of hold there are, and they do not overlap: `in-progress` is
        write-only (§3), so the lease answers for it and a task wearing a stale
        badge with no live lease is offered like any other. An EXPIRED lease
        holds nothing either (§5): the task is offered, and the claim that
        follows steals the lease.

        A held LABEL is not the last word: a task whose operator has already
        replied is requeued by derivation (§6, `requeued`) and rejoins the
        candidates — it still has to clear its dependencies like any other."""
        still_held = set(HELD_LABELS) - set(self.requeued(live))
        if self.labels & still_held or self.number in live:
            return "held"
        if self._blockers:
            blocked = any(str(b.get("state", "")).upper() == "OPEN"
                          for b in self._blockers)
            return "held" if blocked else "startable"
        return None if self.depends_on is not None else "startable"

    # --- the two mutations a queue read performs ------------------------------

    def hydrate(self, window: list[Json]) -> None:
        """Attach the trailing comment window the hydration pass fetched."""
        self.comments = list(window)

    def reclaim(self) -> None:
        """Fold an APPLIED reclaim back in: the label swap `apply_reconcile` just
        wrote, and nothing more, so the drain that reconciled classifies the
        reconciled state without paying for a second fetch."""
        self.labels = (self.labels - {"in-progress"}) | {"needs-decision"}


@dataclasses.dataclass(frozen=True)
class Candidate:
    """One task the startable filter classified: the task, and the verdict.

    It used to be a positional tuple whose LENGTH depended on a boolean flag —
    five elements with `include_body=True`, four without — so callers read it as
    `for number, title, _created, state, body in rows` and the filter itself
    wrote a verdict back by magic index (`rows[idx][3] = "held"`). The `Lease` in
    this same package got an object; the candidate had not.

    It then became a dataclass that COPIED four fields off the node — which is
    why `acquire_next` had to keep a `{number: node}` index alongside the
    candidate list to reach the requeue derivation. Carrying the task instead
    costs nothing and deletes that second index: `cand.task.requeued(live)`.

    `state` is None only while the classification is still waiting on the batched
    `depends-on` resolution; every candidate `Queue.candidates` returns has one."""

    task: Task
    state: str | None

    @property
    def number(self) -> Issue:
        return self.task.number

    @property
    def title(self) -> str:
        return self.task.title

    @property
    def body(self) -> str:
        return self.task.body

    @property
    def startable(self) -> bool:
        """Whether a worker may attempt to claim this. The guard and the CAS
        decide ownership either way — this is the offer, not the verdict."""
        return self.state == "startable"


def cmd_list_startable(args: argparse.Namespace) -> int:
    rows = Queue(args.api).candidates(args.project)
    if rows is None:
        return EXIT_TRANSPORT

    if args.snapshot:
        for c in sorted(rows, key=lambda c: c.number):
            print(f"{c.number}:{c.state}")
    else:
        for c in rows:  # priority-first, then createdAt FIFO
            if c.startable:
                print(f"{c.number}\t{c.title}")
    return EXIT_OK


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


@dataclasses.dataclass(frozen=True)
class QueueRead:
    """One queue read, whole: every open `Task`, the lease state of every claim
    ref, and the commit meta those leases were decoded from.

    The commit meta used to be dropped on the floor — `read` answered
    `(nodes, leases)` — and that cost far more than it saved. `status` needs it
    to decode each holder's worker, message and heartbeat anchor, so it could
    not use `read` at all: it re-ran the same five-step sequence by hand, with
    five None checks and five diagnostics of its own, in another module. That is
    the two-element tuple's classic bill — it has nowhere to grow, so the third
    value comes back as duplicated orchestration somewhere else."""

    tasks: list[Task]
    leases: dict[Issue, Lease]
    commit_meta: CommitMeta

    def by_number(self) -> dict[Issue, Task]:
        """The tasks indexed by issue number — how the reconciler and the claim
        loop reach the task behind a lease or a candidate."""
        return {task.number: task for task in self.tasks}


class Queue:
    """The coordination repo's task queue as this program reads it: the batched
    walk, the lease state that comes with it, comment hydration, and the
    startable filter.

    Six functions took `api` first and are the two halves of one job — reading
    the queue and deciding which of it is startable. `acquire_next` had to inject
    two of them separately to stay testable; one collaborator replaces both.

    The DECISIONS made on what is read — the requeue derivation, the hungry set,
    the section parsing — stay module-level functions: they are pure, they touch
    no transport, and they are tested as such. The decisions about ONE task moved
    onto `Task`, which is what the walk now returns."""

    def __init__(self, api: Api):
        self.api = api

    def open_tasks(self) -> list[Task] | None:
        """Every OPEN kraken-task issue in the repo, across all projects — number,
        title, createdAt, body, labels and native blocked-by — in one paginated
        GraphQL walk, each decoded into a `Task`. Returns the task list, or None
        on transport failure.

        This is the ONE place the wire shape of an issue node is read. Everything
        above it asks the `Task`, so a change to the query below is a change to
        the constructor beside it and to nothing else.

        Deliberately does NOT carry comments. This walk is the hot read: every
        worker's watcher runs it once a minute, so a trailing comment window on every
        node was the queue's first scaling wall — a 200-task queue shipped 5,000
        comment bodies per poll, per worker, to answer a question about a handful of
        them. Both readers that need comments (the requeue derivation and
        `Task.expiries`) only ever ask about tasks that are HELD or LEASED, so the
        window is fetched for exactly those — see `hydrate`."""
        owner, name = self.api.repo.split("/", 1)
        tasks = []
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
            tasks.extend(Task(node) for node in page["nodes"])
            if not page["pageInfo"]["hasNextPage"]:
                return tasks
            cursor = page["pageInfo"]["endCursor"]

    def read(self, now: Epoch | None = None, ttl: int | None = None,
             ) -> QueueRead | None:
        """One queue read: every open kraken-task node (repo-wide, BEFORE any project
        filter) plus the LEASE state of every claim ref. The single fetch the
        reconciler (§6), the startable classification, the claim path and the console
        all consume, so a reader that does several pays for it once. Returns a
        `QueueRead`, or None on transport failure.

        Resolving the lease costs one extra batched call — and only when a ref
        actually exists: `resolve_commit_meta` answers `{}` for an empty ref list
        without a request, so an idle queue reads exactly as cheaply as it did under
        protocol/5, and a busy one pays O(1) in the number of claims, never O(N).

        The comment hydration obeys the same rule and for the same reason: the leases
        have to be known before the hungry set can be (a live lease outranks the
        thread), so it runs last, and it asks for nothing when nothing is held."""
        tasks = self.open_tasks()
        if tasks is None:
            return None
        refs = Refs(self.api)
        claim_refs = refs.all()
        if claim_refs is None:
            return None
        commit_meta = refs.commit_meta(holder_shas(claim_refs))
        if commit_meta is None:
            return None
        leases = lease_state(claim_refs, commit_meta,
                             time.time() if now is None else now,
                             lease_ttl_seconds(ttl))
        if self.hydrate(tasks, leases) is None:
            return None
        return QueueRead(tasks, leases, commit_meta)

    def candidates(self, project: str, read: QueueRead | None = None,
                   ) -> list[Candidate] | None:
        """The shared startable/held classification list-startable, watch's snapshot
        and claim-next all read — one code path so the filter cannot drift between
        them. Returns `Candidate`s ordered priority:high-first then oldest-first by
        createdAt within each tier (a stable sort — see PRIORITY_LABEL), or None on
        transport failure.

        The classification of one task is `Task.state`; this is the page around
        it: the project filter, the ordering, and the one batched call that
        answers every `depends-on` target at once. Every candidate carries its
        whole task, so claim-next can brief a subagent from the win without a
        second fetch and the guard can re-derive the requeue verdict from the
        same task the filter used — the GraphQL walk already paid for both.

        `read` accepts an already-fetched `QueueRead`, which is how claim-next
        classifies the state its reconcile pass just produced without
        re-fetching it."""
        if read is None:
            read = self.read()
            if read is None:
                return None
        live = live_leases(read.leases)
        tasks = [t for t in read.tasks if project in t.projects]
        # priority:high tasks lead; createdAt breaks ties FIFO within each tier. The
        # key sorts on (not-high, createdAt) so the boolean puts the high tier first
        # and the timestamp keeps the older task ahead inside a tier — a scheduling
        # preference layered on top of pure FIFO (see PRIORITY_LABEL).
        tasks.sort(key=lambda t: (PRIORITY_LABEL not in t.labels, t.created))

        rows = [Candidate(task, task.state(live)) for task in tasks]
        # The undecided ones, resolved in ONE batched call rather than a request
        # per candidate: a `depends-on: #N` target's open/closed state is the only
        # thing `Task.state` cannot answer from the task in front of it.
        pending = [(i, task.depends_on) for i, task in enumerate(tasks)
                   if rows[i].state is None]
        if pending:
            dep_open = self._depends_on(sorted({dep for _, dep in pending}))
            if dep_open is None:
                return None
            for i, dep in pending:
                rows[i] = dataclasses.replace(
                    rows[i],
                    state="held" if dep_open.get(dep, False) else "startable")
        return rows

    def hydrate(self, tasks: list[Task],
                leases: dict[Issue, Lease]) -> list[Task] | None:
        """Attach the trailing comment window to the tasks that need one, in
        place, and answer the tasks back — or None on transport failure, which the
        callers propagate exactly like a failed walk.

        This is the second half of the split that took the comment window off the hot
        read (see open_tasks): the walk answers "what is in the queue" for every
        task, and this answers "what does the thread say" for the few whose answer
        depends on it. An idle queue hydrates nothing and pays nothing."""
        hungry = comment_hungry(tasks, leases)
        windows = self.comment_windows(hungry)
        if windows is None:
            return None
        for task in tasks:
            window = windows.get(task.number)
            if window is not None:
                task.hydrate(window)
        return tasks

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

    # --- routing: which projects this repo actually carries -------------------

    def projects(self) -> list[str] | None:
        """Every project:<name> label configured in the repo, sorted, prefix
        stripped — the launch recon points a worker at each. Read from the repo's
        label set (not the open-task walk) so a project with no open task still
        gets a launch line. Returns a sorted name list, or None on transport
        failure."""
        items = self.api.paginated(f"/repos/{self.api.repo}/labels")
        if items is None:
            return None
        return sorted(
            n["name"][len("project:"):]
            for n in items
            if isinstance(n, dict) and str(n.get("name", "")).startswith("project:")
        )

    def verify_project(self, project: str) -> tuple[bool | None, str]:
        """Check that the coordination repo actually carries the `project:<name>`
        label a worker was pointed at. Returns (ok, message): True when the label
        exists, False (with a message naming the configured projects and the fix)
        when it does not, and None when the label read itself failed — a project
        is never declared missing from a read that never landed.

        Routing is entirely client-side (§3): a worker scoped to a label nobody
        uses filters every task out and reads as an empty queue forever, so a
        typo'd or never-created project silently produces a worker that hears
        nothing. Cheap to check once, so check it before the drain rather than
        never."""
        names = self.projects()
        if names is None:
            return (None, "project check: gh-failure stage=labels")
        if project in names:
            return (True, "")
        configured = ", ".join(names) if names else "(none configured)"
        return (False,
                "unknown project: %s has no `project:%s` label, so this worker "
                "would never see a task. Configured projects: %s. Fix the "
                "--project spelling, or create the label with `kraken.py init %s "
                "--project %s`."
                % (self.api.repo, project, configured, self.api.repo, project))

    # --- internals ------------------------------------------------------------

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
