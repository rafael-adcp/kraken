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
#
# The operator console, mechanized (PROTOCOL.md §12): a read-only view — review
# queue, decision queue, in-flight with heartbeat ages, the merged-PR-but-open
# orphan heuristic, and launch recon — computed deterministically so the skill is
# a thin renderer and the data is reusable (`--json`). No write of any kind.
# Reuses the one queue read whole: the walk (titles, hygiene), the claim refs
# (in-flight worker/age/progress) and the state records (what holds each task,
# and the delivery PR). It reads no comment at all — protocol/9 moved the last
# two things that needed one, the requeue verdict and the PR link, into the
# record.

_PR_PARTS_RE = re.compile(r"^https?://(?:www\.)?github\.com/([^/]+)/([^/]+)/pull/(\d+)")


def parse_github_pr_url(pr_url: str) -> tuple[str, str, str] | None:
    """(owner, name, number) parsed from a github.com web pull-request URL, or
    None when the delivery URL isn't one — a non-GitHub delivery (GitLab MR,
    Bitbucket PR, ...) that PROTOCOL.md never forbids, or anything else kraken's
    merge-state check can't understand. Distinct from a transport failure: this
    is a shape check, no network involved."""
    m = _PR_PARTS_RE.search(pr_url or "")
    if m is None:
        return None
    return m.group(1), m.group(2), m.group(3)


def pr_is_merged(api: Api, pr_url: str) -> bool | None:
    """Whether a delivery PR is already merged — the orphan heuristic's only
    signal. Returns True/False; False also covers a delivery URL kraken cannot
    evaluate (not a github.com pull-request URL) — "not confirmed merged",
    never guessed, so a legitimate non-GitHub delivery cannot brick the whole
    status read. None is reserved for an actual gh transport failure on a
    github.com PR URL it did understand — `StatusReport` still propagates that
    as the stage=read failure it actually is. The web `…/pull/N` URL recorded
    in the delivery marker maps to the REST `pulls/N` endpoint, whose
    `merged`/`merged_at` fields are the authoritative merge signal."""
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
    """The queue entries that are dead on arrival, read off the walk the console
    already performed (PROTOCOL.md §2.1): no `project:<name>` label (invisible to
    every worker), or an empty/absent Goal or Acceptance section (a worker claims
    it, then stalls). Returns [{number, title, missing: [...]}], oldest first.

    This is the read-side twin of the arrival-time validator: the same three
    checks, the same wording, decided from the issue bodies the queue walk
    already carries — so it costs no request of its own and needs nothing running
    in the coordination repo. What is missing from ONE task is `Task.missing`;
    this is the scope and the ordering around it.

    A `project` scope filters tasks that HAVE a project label, but never hides a
    task that has none: belonging to no project, it is exactly what the scope
    would bury, and it is the failure this check exists to surface."""
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
    """The operator console's report, computed from one queue read.

    It was a 99-line function taking seven arguments, three of them keyword-only
    and one of them a callable — the last big procedure of the family whose claim
    and next-action halves already became `ClaimAttempt` and `NextAction`. The
    arguments were never really parameters of a call: `api`, the project scope
    and the clock are what a report IS, so they live here, and `of` takes the
    one thing that genuinely varies — the read.

    Each held state gets its own row builder. They answer None on a transport
    failure and `of` propagates it, which is how a console that could not read
    a comment thread exits 20 rather than reporting a queue it did not see.

    `pr_merged` stays injectable and defaults to the real thing. It is the one
    genuinely different collaborator — the merge state of a PR in some OTHER
    repo — and keeping it separate is what lets the merge cases be tested
    without hand-building pull-request payloads."""

    def __init__(self, api: Api, project: str, now: Epoch, *,
                 pr_merged: Callable[[str], bool | None] | None = None):
        self.api = api
        self.project = project
        self.now = now
        self.pr_merged = pr_merged or (lambda url: pr_is_merged(api, url))

    def of(self, read: QueueRead) -> Json | None:
        """The report dict, or None on any transport failure inside it
        (propagated by the caller as exit 20)."""
        # Queue hygiene reads the UNFILTERED walk on purpose: the most valuable
        # case it reports — a task carrying no project label at all — is
        # precisely the one a project scope would hide, since the task belongs
        # to no project.
        hygiene = queue_hygiene(read.tasks, self.project)
        review, decision, in_flight = [], [], []

        for task in self._scoped(read.tasks):
            # The console reads the queue the way a worker does (§3.1, §6): the
            # state record decides, and a task whose thread has moved past it is
            # back in the queue — so it must not keep sitting in the review or
            # decision list waiting for a call already made.
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
        """The tasks this report covers, oldest first — the project scope applied
        and the order the console prints in."""
        if self.project:
            tasks = [t for t in tasks if self.project in t.projects]
        return sorted(tasks, key=lambda t: (t.created, t.number))

    def _reviewed(self, task: Task, record: TaskState) -> Json | None:
        """One awaiting-merge row: the delivered PR link and whether it has
        already been merged (which makes the still-open task an orphan). None on
        a transport failure — the PR read.

        The link comes from the state record's `pr` (§8), which the queue read
        already carried, so this costs no request of its own. Through protocol/8
        it meant paginating the whole comment thread of every delivered task to
        find the newest `delivered` marker — and falling back to a regex over the
        prose when none carried one, which could report a PR a human mentioned in
        passing with the same confidence as the real delivery. There is no
        fallback now: a record with no `pr` is a delivery with no PR, which §8
        allows, and `pr_url: null` says exactly that."""
        pr_url = record.pr
        orphan = False
        merge_state_unknown = False
        if pr_url:
            merged = self.pr_merged(pr_url)
            if merged is None:
                return None
            orphan = bool(merged)
            # A non-GitHub delivery (GitLab MR, Bitbucket PR, ...) is never
            # flagged an orphan — pr_merged already returns False for it — but
            # the operator should still see it wasn't actually checked.
            merge_state_unknown = parse_github_pr_url(pr_url) is None
        return {"number": task.number, "title": task.title,
                "pr_url": pr_url, "orphan": orphan,
                "merge_state_unknown": merge_state_unknown}

    def _in_flight(self, task: Task, lease: Lease,
                   commit_meta: CommitMeta) -> Json:
        """One in-flight row, keyed on the LEASE and never the label (§3): the
        label is written for the human reading the issue list and nothing repairs
        it, so a console that read it would strand a "worker unknown" row on
        every task whose release crashed — forever. Keying on the lease also
        covers the opposite window, a claim whose label has not landed yet.

        An expired lease is not a claim any more: the next drain to read the
        queue steals it, no repair pass involved. The console still says so out
        loud — a lease due to expire in a minute looks exactly like one being
        renewed, and only the operator can tell which of their workers is alive."""
        worker, msg, anchor = claim_meta_of(lease.sha, commit_meta)
        return {"number": task.number, "title": task.title,
                "worker": worker, "heartbeat_anchor": anchor,
                "heartbeat_age_seconds": lease.age, "heartbeat_msg": msg,
                "stale": lease.expired}

    def _projects(self) -> list[str] | None:
        """The launch recon: a scoped report names its own project, an unscoped
        one reads the repo's whole `project:` label set. None on a failed read."""
        return [self.project] if self.project else Queue(self.api).projects()


def cmd_status(args: argparse.Namespace) -> int:
    api, project = args.api, args.project
    # One queue read, the same one a drain performs: nodes, the lease state of
    # every claim ref, and the commit meta the holders are decoded from. The
    # console shares it rather than re-running those steps with None checks of
    # its own, which is why `read` carries the commit meta (see QueueRead).
    #
    # The read comes FIRST and ages its own leases against the server's clock
    # (§5.1); `generated_at` is then stamped from that same clock, which is only
    # knowable once a response has been seen.
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
