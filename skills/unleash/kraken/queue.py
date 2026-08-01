"""Reading the queue: the batched walk, the state records it is classified
against, the requeue derivation, and the startable filter.

Part of the kraken protocol package; see __init__.py."""
from __future__ import annotations

import argparse
import dataclasses
import re
from typing import Iterable, Mapping, Sequence

from .contract import (
    CommitMeta, EXIT_OK, EXIT_TRANSPORT, Epoch, HELD_LABELS, Issue, Json,
    Node, PRIORITY_LABEL, Sha, Worker
)
from .comments import parse_marker
from .transport import Api
from .lease import (
    Lease, holder_shas, lease_state, lease_ttl_seconds, live_leases
)
from .refs import Refs, kraken_ref_items
from .state import (
    NO_RECORD, States, TaskState, holding_state, state_view
)

# --- subcommand: list-startable ---------------------------------------------
#
# Queue fetch and blocked-by check are batched through GraphQL, so an idle poll
# costs a queue-size-independent number of round trips. GraphQL's
# `issues(labels: [...])` is a UNION (unlike REST's AND), so we filter server-side
# on the single "kraken-task" label and match the project label client-side.



# --- the requeue derivation (PROTOCOL.md §6) ---------------------------------
# When anything is said on a held task's thread, the task rejoins the queue. That
# is a property of the record and the comment count, readable at any time, so it
# is DERIVED on the read rather than mutated onto the task: no workflow, no label
# write, and no window where the queue disagrees with the thread.
#
# The whole derivation is `total > record.comments` (§3.1, `TaskState.requeued`).
# The transition wrote down what the thread carried when it put the task down,
# and the walk brings back what it carries now. One integer against another: no
# comment is fetched, no author classified, no body opened.
#
# The rule is the SAME for both held states, and the counter makes it symmetric
# for free: neither state has anything to read.

# --- the issue-form body -----------------------------------------------------
# Pure string readers, kept free of `Task`: the same two are applied to a body
# read straight off REST — `validate` reads one issue, `task_brief` reads the
# issue a resume verdict fetched — where no queue walk and therefore no `Task`
# exists.

NO_RESPONSE_PLACEHOLDER = "_No response_"


def section_body(body: str, heading: str) -> str:
    """The trimmed content under `### HEADING` up to the next `### ` heading (or
    EOF). A hand-written issue lacking the heading yields nothing; an issue-form
    field left blank renders as the literal `_No response_`."""
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


def _comment_total(node: Node) -> int:
    """`comments { totalCount }` off a queue-walk node, floored at 0.

    A node that did not select the field reads as 0, which is the fail-open
    answer: 0 makes every held record look requeued, so a walk that lost the
    field offers tasks a worker will then find nothing to do on and escalate
    (§6) — never one where a delivery is silently buried."""
    value = (node.get("comments") or {}).get("totalCount")
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, value)


class Task:
    """One open kraken-task issue, as the queue walk returned it — and the only
    thing above the walk that knows what a GraphQL issue node looks like.

    The wire shape is decoded ONCE, here, in the constructor. That is what keeps
    the one mutation honest: `reclaim` edits a label set rather than rebuilding
    `{"nodes": [{"name": …}]}` for the next reader to decode again.

    Mutable on purpose, and only in the one way a queue read actually mutates a
    task: an applied reconcile is folded back in (`reclaim`). Everything else is
    a query."""

    def __init__(self, node: Node):
        self.number: Issue = node["number"]
        self.title: str = node.get("title", "")
        self.created: str = node.get("createdAt", "")
        self.body: str = node.get("body") or ""
        self.labels: set[str] = {
            lbl.get("name", "")
            for lbl in (node.get("labels") or {}).get("nodes") or []
        }
        # How many comments the thread carries RIGHT NOW. The requeue derivation
        # compares it against the count the state record froze at the last
        # transition (§3.1), and that is the entire use: no body, no author, no
        # window. One scalar the walk brings back for every task, which is why
        # the derivation costs no call of its own.
        self.comment_total: int = _comment_total(node)
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
        """The HELD labels this task wears, in HELD_LABELS order. A projection
        (§3), and read in exactly one decision: `holding`, for a task that has no
        state record to answer with."""
        return tuple(h for h in HELD_LABELS if h in self.labels)

    # --- what the record says -------------------------------------------------

    def record(self, states: Mapping[Issue, TaskState]) -> TaskState:
        """This task's state record, or `NO_RECORD` when it has none (§3.1)."""
        return states.get(self.number, NO_RECORD)

    def holding(self, states: Mapping[Issue, TaskState]) -> str | None:
        """The state that is holding this task — `needs-decision`,
        `awaiting-merge`, or None when nothing is. The one question the startable
        filter, the claim guard and the console all ask, answered in one place so
        the three cannot drift.

        Where a record exists it decides, and the held LABELS are not consulted
        at all: the record names the state and its `comments` anchor says whether
        the thread has moved on since (§6). Where none exists the label is
        honored — that is the protocol/9 fallback that makes a queue written by
        an older revision safe to read, and it derives no requeue, because there
        is no count to compare against. §6's rule 3 turns such a task into a
        record on the first reconcile, and the requeue works from then on.

        The LEASE is not this question. A live lease holds a task whatever its
        record says, and `Task.state` asks that separately — keeping them apart
        is what lets a claimed task's record go on saying `awaiting-merge` from
        the delivery that was bounced back, without the console counting it
        twice."""
        return holding_state(self.record(states), self.comment_total, self.labels)

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

    def state(self, live: dict[Issue, Sha],
              states: Mapping[Issue, TaskState]) -> str | None:
        """This task's startable/held verdict against the live-lease view and the
        state records — or None when the answer depends on the `depends-on`
        target's state, which the caller resolves for the whole page in one
        batched call.

        Held means: a **record** naming an operator-facing state the thread has
        not moved past (`holding`) OR a **live lease** (the lock). Those are the
        only two kinds of hold there are, and they do not overlap: `in-progress`
        is write-only (§3), so the lease answers for it and a task wearing a
        stale badge with no live lease is offered like any other. An EXPIRED
        lease holds nothing either (§5): the task is offered, and the claim that
        follows steals the lease.

        A held record is not the last word: a task with a comment newer than the
        record is requeued by derivation (§6) and rejoins the candidates — it
        still has to clear its dependencies like any other."""
        if self.holding(states) is not None or self.number in live:
            return "held"
        if self._blockers:
            blocked = any(str(b.get("state", "")).upper() == "OPEN"
                          for b in self._blockers)
            return "held" if blocked else "startable"
        return None if self.depends_on is not None else "startable"

    # --- the one mutation a queue read performs -------------------------------

    def reclaim(self) -> None:
        """Fold an APPLIED reclaim back in: the label swap `apply_reconcile` just
        wrote, and nothing more, so the drain that reconciled classifies the
        reconciled state without paying for a second fetch."""
        self.labels = (self.labels - {"in-progress"}) | {"needs-decision"}


@dataclasses.dataclass(frozen=True)
class Candidate:
    """One task the startable filter classified: the task, and the verdict.

    It carries the whole `Task` rather than a copy of a few of its fields, so a
    reader that needs more than the verdict — `acquire_next` reaching the requeue
    derivation, say — asks `cand.task` instead of keeping a second
    `{number: node}` index beside the candidate list.

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
    ref, the state record of every task that has one, and the commit meta both
    were decoded from.

    The commit meta is carried rather than dropped because `status` decodes each
    holder's worker, message and heartbeat anchor out of it. Without it on the
    read, the console cannot use `read` at all and re-runs the same five-step
    fetch by hand, with five None checks and five diagnostics of its own, in
    another module.

    Leases and records sit side by side because they answer different halves of
    "what is this task": the lease says whether somebody is executing it right
    now, the record says what it is between workers (§3.1). Neither is derivable
    from the other, and every reader here needs both."""

    tasks: list[Task]
    leases: dict[Issue, Lease]
    commit_meta: CommitMeta
    states: dict[Issue, TaskState] = dataclasses.field(default_factory=dict)

    def by_number(self) -> dict[Issue, Task]:
        """The tasks indexed by issue number — how the reconciler and the claim
        loop reach the task behind a lease or a candidate."""
        return {task.number: task for task in self.tasks}

    def record(self, issue: Issue) -> TaskState:
        """One task's record, or `NO_RECORD` — the reconciler's and the claim
        path's way in, so neither spells the `.get(..., NO_RECORD)` default and
        gets the absent case wrong."""
        return self.states.get(issue, NO_RECORD)


class Queue:
    """The coordination repo's task queue as this program reads it: the batched
    walk, the lease state that comes with it, comment hydration, and the
    startable filter.

    One collaborator for the whole job — reading the queue and deciding which of
    it is startable — so `acquire_next` injects a single object to stay testable.

    The DECISIONS made on what is read — the requeue derivation, the hungry set,
    the section parsing — stay module-level functions: they are pure, they touch
    no transport, and they are tested as such. The decisions about ONE task
    belong to `Task`, which is what the walk returns."""

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

        Carries the comment COUNT and not one comment body. This walk is the hot
        read — every worker's watcher runs it once a minute — and `totalCount` is
        one integer per node, which is all the requeue derivation needs against
        the record's anchor (§6). Fetching bodies here would put the whole
        queue's comment volume on every poll of every worker."""
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
                f'comments {{ totalCount }} '
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
        filter), the LEASE state of every claim ref, and the STATE RECORD of every
        task that has one. The single fetch the reconciler (§6), the startable
        classification, the claim path and the console all consume, so a reader that
        does several pays for it once. Returns a `QueueRead`, or None on transport
        failure.

        Three calls, and the third only when a ref actually exists: the issue walk,
        one paginated read of the whole `refs/kraken/` namespace (claims AND
        records — see `kraken_ref_items`), and one batched commit read resolving
        both families at once. `Refs.commit_meta` answers `{}` for an empty ref list
        without a request, so an idle queue pays exactly two calls. No comment is
        read on any path."""
        tasks = self.open_tasks()
        if tasks is None:
            return None
        got = self._ref_view(now, ttl, with_states=True)
        if got is None:
            return None
        leases, commit_meta, states, _state_shas = got
        return QueueRead(tasks, leases, commit_meta, states)

    def lease_view(self, now: Epoch | None = None, ttl: int | None = None,
                   ) -> tuple[dict[Issue, Lease], CommitMeta,
                              dict[Issue, Sha]] | None:
        """The ladder half of `read`: every claim ref's lease, aged against the
        server's clock, the commit meta it was decoded from, and the state refs
        as raw `{issue: sha}` — WITHOUT the issue walk and WITHOUT resolving
        those records into `TaskState`s.

        Returns `(leases, commit_meta, state_shas)`, or None on transport failure.

        Separate from `read` because one question genuinely does not need the
        queue: "does this worker already hold a claim?" (§5) is answered by the
        LADDER, and the ladder is what arbitrates it. A caller that asks only
        that should not pay for a paginated issue walk to find out — see
        `claim.open_claim_of`.

        The state shas ride along because parsing them out of the payload is
        pure — the matching-refs read already carried them, and `state_ref_shas`
        spends nothing. What is NOT done here is resolving their commits: a
        worker holding nothing needs no record, and batching every record of the
        queue into this read would put a commit read on the one path that had
        none. The caller resolves the one record it turns out to need
        (`States.at`), which is zero or one per invocation."""
        got = self._ref_view(now, ttl, with_states=False)
        if got is None:
            return None
        leases, commit_meta, _states, state_shas = got
        return (leases, commit_meta, state_shas)

    def _ref_view(self, now: Epoch | None, ttl: int | None, *,
                  with_states: bool,
                  ) -> tuple[dict[Issue, Lease], CommitMeta,
                             dict[Issue, TaskState], dict[Issue, Sha]] | None:
        """The `refs/kraken/` namespace, decoded: leases, the commit meta behind
        them, the state records when asked for, and the raw state shas either
        way. None on transport failure.

        One matching-refs read serves both families, and ONE batched commit read
        resolves every sha either of them named, so adding records to a queue read
        costs no extra round trip: the aliases just get longer, and `Api.aliased`
        already chunks them.

        `with_states` buys the COMMITS, not the shas: naming which tasks have a
        record is a pure parse of the payload above, so it happens either way and
        a caller that wants one record can resolve it alone."""
        refs = Refs(self.api)
        items = kraken_ref_items(self.api)
        if items is None:
            return None
        claim_refs = refs.all(items)
        if claim_refs is None:
            return None
        state_shas = States(self.api).all(items)
        if state_shas is None:
            return None
        commit_meta = refs.commit_meta(
            holder_shas(claim_refs)
            + (sorted(state_shas.values()) if with_states else []))
        if commit_meta is None:
            return None
        # The server's clock, not this machine's (§5.1): the lease timestamps
        # about to be aged are commit dates GitHub stamped, and the reads above
        # have already been told what time GitHub thinks it is.
        leases = lease_state(claim_refs, commit_meta,
                             self.api.server_now() if now is None else now,
                             lease_ttl_seconds(ttl))
        states = state_view(state_shas, commit_meta) if with_states else {}
        return (leases, commit_meta, states, state_shas)

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

        rows = [Candidate(task, task.state(live, read.states)) for task in tasks]
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
                    targets: Iterable[Issue]) -> dict[Issue, bool] | None:
        """Resolve every `depends-on: #N` fallback target's open/closed state
        through the batched fan-out (one aliased `iN: issue(number: N) { state }`
        field per distinct target), never one call per candidate. Returns
        {number: is_open}, or None on transport failure.

        The targets are materialized because they are read twice — once to build
        the fields, once to read the answers back — and a caller is entitled to
        hand this a generator."""
        targets = list(targets)
        fields = [f"i{n}: issue(number: {n}) {{ state }}" for n in targets]
        repo_obj = self.api.aliased(fields)
        if repo_obj is None:
            return None
        return {
            n: str((repo_obj.get(f"i{n}") or {}).get("state", "")).upper() == "OPEN"
            for n in targets
        }
