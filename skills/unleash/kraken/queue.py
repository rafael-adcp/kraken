"""Reading the queue: the batched walk, the state records it is classified
against, the requeue derivation, and the startable filter.

Part of the kraken protocol package; see __init__.py."""
from __future__ import annotations

import argparse
import dataclasses
import re
from typing import Iterable, Mapping

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

# --- the issue-form body -----------------------------------------------------
# Free functions rather than `Task` methods: `validate` and `task_brief` apply
# them to bodies read straight off REST, where no `Task` exists.

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


def section_text(body: str, heading: str) -> str:
    """The trimmed content under `### HEADING`, "" when blank or absent."""
    content = section_body(body, heading)
    return "" if is_empty_section(content) else content.strip()


def missing_requirements(labels: Iterable[str], body: str) -> list[str]:
    """What makes a queue entry dead on arrival (§2.1): any of "project label",
    "Goal", "Acceptance". Empty when a worker can start it."""
    missing = []
    if not any(name.startswith("project:") for name in labels):
        missing.append("project label")
    for heading in ("Goal", "Acceptance"):
        if not section_text(body, heading):
            missing.append(heading)
    return missing


# --- one task, as the startable filter sees it -------------------------------

DEPENDS_ON_RE = re.compile(r"^depends-on: *#([0-9]+)", re.MULTILINE)


def _comment_total(node: Node) -> int:
    """`comments { totalCount }` off a walk node. A missing field reads as 0,
    which fails CLOSED: 0 is below every anchor, so nothing is requeued."""
    value = (node.get("comments") or {}).get("totalCount")
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, value)


class Task:
    """One open kraken-task issue from the queue walk. The GraphQL node is
    decoded here and nowhere else."""

    def __init__(self, node: Node):
        self.number: Issue = node["number"]
        self.title: str = node.get("title", "")
        self.created: str = node.get("createdAt", "")
        self.body: str = node.get("body") or ""
        self.labels: set[str] = {
            lbl.get("name", "")
            for lbl in (node.get("labels") or {}).get("nodes") or []
        }
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
        """The held labels this task wears, in HELD_LABELS order."""
        return tuple(h for h in HELD_LABELS if h in self.labels)

    # --- what the record says -------------------------------------------------

    def record(self, states: Mapping[Issue, TaskState]) -> TaskState:
        """This task's state record, or `NO_RECORD` when it has none (§3.1)."""
        return states.get(self.number, NO_RECORD)

    def holding(self, states: Mapping[Issue, TaskState]) -> str | None:
        """The state holding this task, or None (see `holding_state`). The lease
        is a separate question, asked by `state`."""
        return holding_state(self.record(states), self.comment_total, self.labels)

    # --- what the body says ---------------------------------------------------

    @property
    def depends_on(self) -> Issue | None:
        """The `depends-on: #N` target in the body, or None — the fallback for a
        task with no native blocked-by link."""
        m = DEPENDS_ON_RE.search(self.body)
        return int(m.group(1)) if m else None

    @property
    def missing(self) -> list[str]:
        """What makes this entry dead on arrival (§2.1)."""
        return missing_requirements(self.labels, self.body)

    # --- the startable verdict ------------------------------------------------

    def state(self, live: dict[Issue, Sha],
              states: Mapping[Issue, TaskState]) -> str | None:
        """"held" or "startable" — or None when it hangs on a `depends-on`
        target, which the caller resolves for the whole page at once.

        Only two things hold a task: a record `holding` it, or a live lease. The
        `in-progress` label is write-only (§3) and an expired lease holds
        nothing (§5)."""
        if self.holding(states) is not None or self.number in live:
            return "held"
        if self._blockers:
            blocked = any(str(b.get("state", "")).upper() == "OPEN"
                          for b in self._blockers)
            return "held" if blocked else "startable"
        return None if self.depends_on is not None else "startable"

    # --- the one mutation a queue read performs -------------------------------

    def reclaim(self) -> None:
        """Fold an applied reclaim's label swap back into this read."""
        self.labels = (self.labels - {"in-progress"}) | {"needs-decision"}


@dataclasses.dataclass(frozen=True)
class Candidate:
    """One classified task: the task, and its verdict."""

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
        """An offer to attempt a claim; the CAS decides ownership."""
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
    """Decode one claim ref's commit into (worker, msg, anchor_iso). The commit
    date is the liveness clock, so nothing on the issue timeline can make a
    claim look alive. Unreadable pieces are None."""
    commit = commit_meta.get(sha) or {}
    payload = parse_marker(commit.get("message") or "") or {}
    worker = payload.get("worker") or None
    msg = payload.get("msg") or None
    anchor = commit.get("committedDate") or None
    return worker, msg, anchor


@dataclasses.dataclass(frozen=True)
class QueueRead:
    """One queue read: every open task, every lease (who is executing), every
    state record (what the task is between workers, §3.1), and the commit meta
    they were decoded from, which `status` reads heartbeats out of."""

    tasks: list[Task]
    leases: dict[Issue, Lease]
    commit_meta: CommitMeta
    states: dict[Issue, TaskState] = dataclasses.field(default_factory=dict)

    def by_number(self) -> dict[Issue, Task]:
        """The tasks indexed by issue number."""
        return {task.number: task for task in self.tasks}

    def record(self, issue: Issue) -> TaskState:
        """One task's record, or `NO_RECORD`."""
        return self.states.get(issue, NO_RECORD)


class Queue:
    """The coordination repo's task queue: the batched walk, the leases and
    records that come with it, and the startable filter. Decisions about one
    task belong to `Task`."""

    def __init__(self, api: Api):
        self.api = api

    def open_tasks(self) -> list[Task] | None:
        """Every open kraken-task issue, all projects, in one paginated GraphQL
        walk; None on transport failure. GraphQL's `labels:` filter is a UNION,
        so only `kraken-task` is filtered server-side.

        This is the hot read (every watcher, every minute), so it carries the
        comment COUNT and never a comment body."""
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
        """One repo-wide queue read (before any project filter), or None on
        transport failure. Three calls — the walk, the `refs/kraken/` namespace,
        one batched commit read — and an idle queue skips the third."""
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
        """`read` without the issue walk and without resolving records:
        `(leases, commit_meta, state_shas)`, or None on transport failure. For
        callers asking only "does this worker hold a claim?" (§5)."""
        got = self._ref_view(now, ttl, with_states=False)
        if got is None:
            return None
        leases, commit_meta, _states, state_shas = got
        return (leases, commit_meta, state_shas)

    def _ref_view(self, now: Epoch | None, ttl: int | None, *,
                  with_states: bool,
                  ) -> tuple[dict[Issue, Lease], CommitMeta,
                             dict[Issue, TaskState], dict[Issue, Sha]] | None:
        """The `refs/kraken/` namespace decoded, or None on transport failure.
        One batched commit read resolves claims and, `with_states`, records."""
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
        # The server's clock, not ours (§5.1): GitHub stamped these dates.
        leases = lease_state(claim_refs, commit_meta,
                             self.api.server_now() if now is None else now,
                             lease_ttl_seconds(ttl))
        states = state_view(state_shas, commit_meta) if with_states else {}
        return (leases, commit_meta, states, state_shas)

    def candidates(self, project: str, read: QueueRead | None = None,
                   ) -> list[Candidate] | None:
        """`project`'s tasks classified, priority:high first then oldest first;
        None on transport failure. Pass `read` to classify an existing read."""
        if read is None:
            read = self.read()
            if read is None:
                return None
        live = live_leases(read.leases)
        tasks = [t for t in read.tasks if project in t.projects]
        tasks.sort(key=lambda t: (PRIORITY_LABEL not in t.labels, t.created))

        rows = [Candidate(task, task.state(live, read.states)) for task in tasks]
        # Every undecided `depends-on` target, in ONE batched call.
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
        """Every configured `project:<name>`, sorted, or None on transport
        failure. Read from the label set, so a project with no open task still
        counts."""
        items = self.api.paginated(f"/repos/{self.api.repo}/labels")
        if items is None:
            return None
        return sorted(
            n["name"][len("project:"):]
            for n in items
            if isinstance(n, dict) and str(n.get("name", "")).startswith("project:")
        )

    def verify_project(self, project: str) -> tuple[bool | None, str]:
        """(ok, message): whether the repo carries `project:<name>`. `ok` is
        None when the label read failed — a project is never declared missing
        from a read that never landed."""
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
        """{number: is_open} for every target in one aliased call, or None on
        transport failure."""
        targets = list(targets)
        fields = [f"i{n}: issue(number: {n}) {{ state }}" for n in targets]
        repo_obj = self.api.aliased(fields)
        if repo_obj is None:
            return None
        return {
            n: str((repo_obj.get(f"i{n}") or {}).get("state", "")).upper() == "OPEN"
            for n in targets
        }
