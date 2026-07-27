"""The read-only console: what an operator needs to see in one place.

Part of the kraken protocol package; see __init__.py."""
from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
import time
from typing import Callable, Sequence

from .contract import (
    CommentRecord, CommitMeta, EXIT_OK, EXIT_TRANSPORT, Epoch, Issue, Json,
    Node
)
from .comments import parse_marker
from .transport import Api
from .lease import Lease, live_leases
from .queue import (
    Queue, QueueRead, claim_meta_of,
    is_empty_section, label_names_of, project_names_of, requeued_labels,
    section_body
)
from .render import render_status
from .claim import list_projects

# --- subcommand: status ------------------------------------------------------
#
# The operator console, mechanized (PROTOCOL.md §12): a read-only view — review
# queue, decision queue, in-flight with heartbeat ages, the merged-PR-but-open
# orphan heuristic, and launch recon — computed deterministically so the skill is
# a thin renderer and the data is reusable (`--json`). No write of any kind.
# Reuses fetch_open_tasks (queue), the claim refs (in-flight worker/age/progress),
# the held/leased comment hydration (the requeue derivation), and paginated
# comment reads only for awaiting-merge tasks (the PR link).

# The LEGACY free-text fallback, and nothing else (§8). The delivery URL is a
# structured field — the `delivered` marker's `pr` — and reading it off the prose
# is exactly the text coupling the marker retired (#24, #32, #48): a regex over a
# comment thread happily returns a PR a human mentioned in passing, another
# task's PR, or some other repo's `/pull/N`. Kept only for threads delivered
# before the marker carried `pr`, always reported as legacy, and removable at the
# next backward-incompatible protocol bump.
_PR_URL_RE = re.compile(r"https?://\S+?/pull/\d+")


def parse_pr_url(
    records: Sequence[CommentRecord],
) -> tuple[str | None, str | None]:
    """The delivery PR URL of an awaiting-merge task, as `(url, source)`.

    The structured field is the source of truth (§8): the `pr` of the newest
    `delivered` marker in the thread, source `"marker"`. The prose is read ONLY
    when no `delivered` marker carries a usable `pr` — a legacy thread, or a
    delivery posted without a PR — and comes back tagged `"legacy-free-text"` so
    every reader can say out loud that the link was guessed rather than
    recorded. `(None, None)` when no PR was recorded at all.

    A marker line is machine payload, never prose: it is skipped by the regex
    pass, so a delivered marker with no `pr` can never seed the fallback from
    its own JSON, and a `note`/`released` marker that happens to carry a `pr`
    key is not a delivery."""
    from_marker = None
    fallback = None
    for rec in records:  # server order — keep overwriting so the newest wins
        for raw in (rec.get("body") or "").split("\n"):
            marker = parse_marker(raw)
            if marker is not None:
                if marker.get("type") == "delivered":
                    pr = marker.get("pr")
                    if isinstance(pr, str) and pr.strip():
                        from_marker = pr.strip()
                continue
            m = _PR_URL_RE.search(raw)
            if m:
                fallback = m.group(0)
    if from_marker:
        return from_marker, "marker"
    if fallback:
        return fallback, "legacy-free-text"
    return None, None


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
    never guessed, so a legitimate non-GitHub delivery no longer bricks the
    whole status read. None is reserved for an actual gh transport failure on a
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


def queue_hygiene(nodes: Sequence[Node], project: str = "") -> list[Json]:
    """The queue entries that are dead on arrival, read off the walk the console
    already performed (PROTOCOL.md §2.1): no `project:<name>` label (invisible to
    every worker), or an empty/absent Goal or Acceptance section (a worker claims
    it, then stalls). Returns [{number, title, missing: [...]}], oldest first.

    This is the read-side twin of the arrival-time validator: the same three
    checks, the same wording, decided from the issue bodies the queue walk
    already carries — so it costs no request of its own and needs nothing running
    in the coordination repo.

    A `project` scope filters tasks that HAVE a project label, but never hides a
    task that has none: belonging to no project, it is exactly what the scope
    would bury, and it is the failure this check exists to surface."""
    out = []
    for node in sorted(nodes, key=lambda n: (n.get("createdAt", ""), n.get("number", 0))):
        projects = project_names_of(node)
        if projects and project and project not in projects:
            continue
        body = node.get("body") or ""
        missing = []
        if not projects:
            missing.append("project label")
        if is_empty_section(section_body(body, "Goal")):
            missing.append("Goal")
        if is_empty_section(section_body(body, "Acceptance")):
            missing.append("Acceptance")
        if missing:
            out.append({"number": node["number"], "title": node.get("title", ""),
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
        hygiene = queue_hygiene(read.nodes, self.project)
        live = live_leases(read.leases)
        review, decision, in_flight = [], [], []

        for node in self._scoped(read.nodes):
            # The console reads the queue the way a worker does (§6): a task
            # whose operator has already replied is back in the queue, so it
            # must not keep sitting in the review or decision list waiting for a
            # call already made.
            labels = [l for l in label_names_of(node)
                      if l not in requeued_labels(node, live)]
            if "awaiting-merge" in labels:
                row = self._reviewed(node)
                if row is None:
                    return None
                review.append(row)
            elif "needs-decision" in labels:
                decision.append({"number": node["number"],
                                 "title": node.get("title", "")})
            elif node["number"] in read.leases:
                in_flight.append(self._in_flight(
                    node, read.leases[node["number"]], read.commit_meta))

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

    def _scoped(self, nodes: Sequence[Node]) -> list[Node]:
        """The tasks this report covers, oldest first — the project scope applied
        and the order the console prints in."""
        if self.project:
            nodes = [n for n in nodes if self.project in project_names_of(n)]
        return sorted(nodes,
                      key=lambda n: (n.get("createdAt", ""), n.get("number", 0)))

    def _reviewed(self, node: Node) -> Json | None:
        """One awaiting-merge row: the delivered PR link, where it came from, and
        whether it has already been merged (which makes the still-open task an
        orphan). None on a transport failure — the thread read or the PR read."""
        records = self.api.comment_records(node["number"])
        if records is None:
            return None
        pr_url, pr_source = parse_pr_url(records)
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
        return {"number": node["number"], "title": node.get("title", ""),
                "pr_url": pr_url, "pr_source": pr_source, "orphan": orphan,
                "merge_state_unknown": merge_state_unknown}

    def _in_flight(self, node: Node, lease: Lease,
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
        return {"number": node["number"], "title": node.get("title", ""),
                "worker": worker, "heartbeat_anchor": anchor,
                "heartbeat_age_seconds": lease.age, "heartbeat_msg": msg,
                "stale": lease.expired}

    def _projects(self) -> list[str] | None:
        """The launch recon: a scoped report names its own project, an unscoped
        one reads the repo's whole `project:` label set. None on a failed read."""
        return [self.project] if self.project else list_projects(self.api)


def cmd_status(args: argparse.Namespace) -> int:
    api, project = args.api, args.project
    now = time.time()
    # One queue read, the same one a drain performs: nodes, the lease state of
    # every claim ref, and the commit meta the holders are decoded from. The
    # console used to run those four steps itself, with four None checks of its
    # own, only because `read` threw the commit meta away.
    read = Queue(api).read(now=now)
    if read is None:
        print("status: gh-failure stage=list", file=sys.stderr)
        return EXIT_TRANSPORT

    report = StatusReport(api, project, now).of(read)
    if report is None:
        print("status: gh-failure stage=read", file=sys.stderr)
        return EXIT_TRANSPORT

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render_status(report))
    return EXIT_OK
