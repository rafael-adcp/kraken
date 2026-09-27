"""The read-only console: what an operator needs to see in one place.

Part of the kraken protocol package; see __init__.py."""
from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
from typing import Callable, Sequence

from .contract import (
    CommitMeta, EXIT_OK, EXIT_TRANSPORT, Epoch, Json
)
from .transport import Api
from .lease import Lease
from .state import TaskState
from .queue import Queue, QueueRead, Task, claim_meta_of
from .render import render_status

# --- subcommand: status ------------------------------------------------------
# The operator console (§12): read-only, computed from one queue read, and it
# reads no comment at all.

_PR_PARTS_RE = re.compile(r"^https?://(?:www\.)?github\.com/([^/]+)/([^/]+)/pull/(\d+)")


def parse_github_pr_url(pr_url: str) -> tuple[str, str, str] | None:
    """(owner, name, number) of a github.com pull-request URL, or None — e.g.
    for a GitLab MR, which the protocol allows."""
    m = _PR_PARTS_RE.search(pr_url or "")
    if m is None:
        return None
    return m.group(1), m.group(2), m.group(3)


def pr_is_merged(api: Api, pr_url: str) -> bool | None:
    """Whether a delivery PR is merged. False also for a URL this cannot check
    ("not confirmed", so a non-GitHub delivery never breaks status); None only
    on a transport failure."""
    parts = parse_github_pr_url(pr_url)
    if parts is None:
        return False
    owner, name, number = parts
    data = api.json("GET", f"/repos/{owner}/{name}/pulls/{number}")
    if data is None:
        return None
    return (bool(data.get("merged_at")) or bool(data.get("merged"))
            or str(data.get("state", "")).upper() == "MERGED")


def queue_hygiene(tasks: Sequence[Task], project: str = "") -> list[Json]:
    """[{number, title, missing}] for dead-on-arrival entries (§2.1), oldest
    first. A `project` scope never hides a task with no project label at all:
    that is exactly the failure this check exists to surface."""
    out = []
    for task in sorted(tasks, key=lambda t: (t.created, t.number)):
        if task.projects and project and project not in task.projects:
            continue
        missing = task.missing
        if missing:
            out.append({"number": task.number, "title": task.title,
                        "missing": missing})
    return out


class StatusReport:
    """The operator console's report, computed from one queue read. A failed
    PR or label read makes it None, so status exits 20 rather than report a
    queue it did not see. `pr_merged` is injectable for tests."""

    def __init__(self, api: Api, project: str, now: Epoch, *,
                 pr_merged: Callable[[str], bool | None] | None = None):
        self.api = api
        self.project = project
        self.now = now
        self.pr_merged = pr_merged or (lambda url: pr_is_merged(api, url))

    def of(self, read: QueueRead) -> Json | None:
        """The report dict, or None on any transport failure inside it
        (propagated by the caller as exit 20)."""
        # Unfiltered on purpose: see `queue_hygiene`.
        hygiene = queue_hygiene(read.tasks, self.project)
        review, decision, in_flight = [], [], []

        for task in self._scoped(read.tasks):
            # As a worker reads it (§3.1): a requeued task leaves these lists.
            holding = task.holding(read.states)
            if holding == "awaiting-merge":
                row = self._reviewed(task, task.record(read.states))
                if row is None:
                    return None
                review.append(row)
            elif holding == "needs-decision":
                decision.append({"number": task.number, "title": task.title})
            elif task.number in read.leases:
                in_flight.append(self._in_flight(
                    task, read.leases[task.number], read.commit_meta))

        projects = self._projects()
        if projects is None:
            return None
        return {
            "repo": self.api.repo,
            "project": self.project or None,
            "generated_at": datetime.datetime.fromtimestamp(
                self.now, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "review_queue": review,
            "decision_queue": decision,
            "in_flight": in_flight,
            "orphans": [r["number"] for r in review if r["orphan"]],
            "queue_hygiene": hygiene,
            "projects": projects,
        }

    def _scoped(self, tasks: Sequence[Task]) -> list[Task]:
        """The tasks in scope, oldest first."""
        if self.project:
            tasks = [t for t in tasks if self.project in t.projects]
        return sorted(tasks, key=lambda t: (t.created, t.number))

    def _reviewed(self, task: Task, record: TaskState) -> Json | None:
        """One awaiting-merge row; a merged PR on a still-open task makes it an
        orphan. None when the PR read fails."""
        pr_url = record.pr
        orphan = False
        merge_state_unknown = False
        if pr_url:
            merged = self.pr_merged(pr_url)
            if merged is None:
                return None
            orphan = bool(merged)
            # A non-GitHub delivery is never an orphan, but say it went unchecked.
            merge_state_unknown = parse_github_pr_url(pr_url) is None
        return {"number": task.number, "title": task.title,
                "pr_url": pr_url, "orphan": orphan,
                "merge_state_unknown": merge_state_unknown}

    def _in_flight(self, task: Task, lease: Lease,
                   commit_meta: CommitMeta) -> Json:
        """One in-flight row, keyed on the lease, never the write-only label
        (§3). An expired lease is still shown, flagged `stale`."""
        worker, msg, anchor = claim_meta_of(lease.sha, commit_meta)
        return {"number": task.number, "title": task.title,
                "worker": worker, "heartbeat_anchor": anchor,
                "heartbeat_age_seconds": lease.age, "heartbeat_msg": msg,
                "stale": lease.expired}

    def _projects(self) -> list[str] | None:
        """The launch recon: the scoped project, or every configured one."""
        return [self.project] if self.project else Queue(self.api).projects()


def cmd_status(args: argparse.Namespace) -> int:
    api, project = args.api, args.project
    # The read comes first: the server's clock is only known after a response.
    read = Queue(api).read()
    if read is None:
        print("status: gh-failure stage=list", file=sys.stderr)
        return EXIT_TRANSPORT
    now = api.server_now()

    report = StatusReport(api, project, now).of(read)
    if report is None:
        print("status: gh-failure stage=read", file=sys.stderr)
        return EXIT_TRANSPORT

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render_status(report))
    return EXIT_OK
