"""Standing a coordination repo up, and the passes its workflows invoke.

Part of the kraken protocol package; see __init__.py."""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Sequence

from .contract import (
    CommentRecord, EXIT_OK, EXIT_TRANSPORT, EXIT_USAGE, SKILL_DIR
)
from .comments import make_marker, parse_marker
from .refs import Refs
from .queue import is_empty_section, section_body
from .render import render_init

# --- subcommand: init --------------------------------------------------------
# The bootstrap `init`, single-sourced here so the skill and the program never
# disagree on the asset set or the label canon.

def bundled_asset(name: str) -> str:
    """The raw bytes of a bundled asset shipped in this skill's folder — what
    `init` commits into the coordination repo."""
    with open(os.path.join(SKILL_DIR, name), "rb") as fh:
        return fh.read()


# Each bundled asset init commits: (bundled filename, destination path in the
# coordination repo, create commit message). Under protocol/5 the coordination
# repo executes nothing, so this is a single file: the issue form that shapes a
# task on the way in.
INIT_ASSETS = (
    ("task-template.yml", ".github/ISSUE_TEMPLATE/task.yml",
     "chore: add kraken task template"),
)

# Assets an earlier kraken release installed and this protocol revision retired.
# `init` DELETES each one it finds: a cron left running against retired semantics
# keeps mutating labels behind every worker's back, which is strictly worse than
# a missing file. The second element is a sentinel that must appear in the file's
# bytes for the delete to happen — proof the file is kraken's own install and not
# something the operator wrote at the same path, so a hand-written workflow is
# never destroyed by a bootstrap command.
OBSOLETE_ASSETS = (
    (".github/workflows/reclaim-stale.yml", b"for kraken. Installed by"),
    (".github/workflows/requeue-on-reply.yml", b"for kraken. Installed by"),
    (".github/workflows/cleanup-closed.yml", b"for kraken. Installed by"),
    (".github/workflows/validate-task.yml", b"for kraken. Installed by"),
    (".github/kraken.py", b"kraken.py \xe2\x80\x94 the bundled worker-side transitions"),
)

# The canonical state-machine labels — (name, color, description). The labels UI
# IS kraken's dashboard, so colors trace the flow left to right: blue queued ->
# yellow working -> red needs-you / green ready-to-land. The authoritative home
# for PROTOCOL.md §3's SHOULD colors; init upserts with --force.
CANONICAL_LABELS = (
    ("kraken-task", "1D76DB", "A unit of work for a kraken worker — the queue"),
    ("in-progress", "FBCA04", "Claimed by a worker and being executed"),
    ("needs-decision", "D93F0B",
     "Blocked on your decision — answer, then remove the label to requeue"),
    ("awaiting-merge", "0E8A16",
     "Delivered as a draft PR — waiting for your review and merge"),
    ("priority:high", "B60205",
     "Claimed ahead of normal tasks — a scheduling preference, not a state"),
)
PROJECT_LABEL_COLOR = "5319E7"
PROJECT_LABEL_DESC = (
    "Canonical project identity — a worker's --project filters on this"
)


def cmd_init(args: argparse.Namespace) -> int:
    """Stand up (or repair) a coordination repo: verify-or-create it private,
    install the bundled assets, prune the retired ones, and upsert the canonical
    labels. Create-only: an asset that already exists is left exactly as it is,
    hand edits included — nothing in the coordination repo is executed any more
    (protocol/5), so a file that differs from the bundled copy is the operator's
    business, not a hazard to police. Re-running init on an older coordination
    repo is also its MIGRATION: assets this protocol revision retired are deleted
    (OBSOLETE_ASSETS). Idempotent; touches no issues. Exit 0 on success, 20 on
    any gh/transport failure."""
    api, project = args.api, args.project
    report = {
        "repo": api.repo,
        "repo_status": "exists",
        "assets": [],
        "labels": [],
        "project": project or None,
    }

    # 1. Verify or create the repo (private).
    if not api.repo_exists():
        if not api.repo_create_private():
            print(f"init: gh-failure stage=repo repo={api.repo}", file=sys.stderr)
            return EXIT_TRANSPORT
        report["repo_status"] = "created"

    # 2. Install the bundled assets, create-only — an existing file is reported
    #    and left alone.
    for name, dest, message in INIT_ASSETS:
        try:
            bundled = bundled_asset(name)
        except OSError:
            print(f"init: missing bundled asset {name}", file=sys.stderr)
            return EXIT_USAGE
        current, _sha = api.get_content_meta(dest)
        if current is None:
            if not api.put_content(dest, bundled, message):
                print(f"init: gh-failure stage=asset path={dest}", file=sys.stderr)
                return EXIT_TRANSPORT
            status = "created"
        else:
            status = "present"
        report["assets"].append({"path": dest, "status": status})

    # 3. Prune the assets an earlier release installed and this revision retired,
    #    so re-running init is also the migration path for a coordination repo
    #    that was stood up before them.
    for dest, sentinel in OBSOLETE_ASSETS:
        current, sha = api.get_content_meta(dest)
        if current is None or sha is None or sentinel not in current:
            continue  # absent, unreadable, or not ours to delete
        if not api.delete_content(dest, sha,
                                 f"chore: remove retired kraken asset {dest}"):
            print(f"init: gh-failure stage=prune path={dest}", file=sys.stderr)
            return EXIT_TRANSPORT
        report["assets"].append({"path": dest, "status": "removed"})

    # 4. Upsert the canonical labels (+ the project label when scoped).
    labels = list(CANONICAL_LABELS)
    if project:
        labels.append((f"project:{project}", PROJECT_LABEL_COLOR, PROJECT_LABEL_DESC))
    for lname, color, desc in labels:
        if not api.label_upsert(lname, color, desc):
            print(f"init: gh-failure stage=label label={lname}", file=sys.stderr)
            return EXIT_TRANSPORT
        report["labels"].append(lname)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render_init(report))
    return EXIT_OK


# The issue-form headings the bundled task-template produces, and the placeholder
# GitHub renders for a blank field. Section detection keys on these.
VALIDATION_MARKER = {"type": "validation"}

# The actionable items the validator lists, one per missing requirement. Single
# copy so the message stays consistent between the workflow and its tests.
VALIDATE_PROJECT_MISSING = (
    "- Add a `project:<name>` label. Workers are scoped to one project and never "
    "see a task without it, so an unlabeled task sits invisible in the queue forever."
)
VALIDATE_GOAL_MISSING = (
    "- Fill in the **Goal** section (the `### Goal` heading). Describe the desired "
    "end state as an outcome — it is what the worker plans toward."
)
VALIDATE_ACCEPTANCE_MISSING = (
    "- Fill in the **Acceptance** section (the `### Acceptance` heading). Give "
    "executable, observable proof the Goal was met — a worker must run it for real "
    "before delivering."
)


def validation_body(missing: Sequence[str]) -> str:
    """The one actionable comment the validator posts, tagged with the protocol/3
    validation marker so the debounce can find its own prior comment. It informs
    only — never blocks, closes, or relabels the task."""
    return "\n\n".join([
        "> 🐙 **Kraken task validator** — this task isn't ready for a worker to pick up yet.",
        "Please fix the following so it can be claimed (this gate only informs; "
        "it never holds, closes, or relabels your task):\n" + "\n".join(missing),
        "Once fixed, this check clears itself — no action needed here.",
        make_marker(VALIDATION_MARKER),
    ])


def latest_validation_comment(records: Sequence[CommentRecord]) -> str | None:
    """The body of the newest prior validation comment (carrying the validation
    marker) in the thread, or None when none exists — the debounce anchor."""
    latest = None
    for rec in records:  # server order: keep the newest match
        body = rec.get("body") or ""
        if any((parse_marker(l) or {}).get("type") == "validation"
               for l in body.split("\n")):
            latest = body
    return latest


def cmd_validate(args: argparse.Namespace) -> int:
    """Flag a queue entry missing its project label, Goal, or Acceptance
    (validate-task.yml). Reads the issue's live labels and body, and on any
    missing requirement posts ONE actionable comment naming exactly what to fix;
    a compliant task gets none (no noise on the happy path, and the same exit
    once the operator fixes what was flagged). Debounced: a re-run whose missing
    set is unchanged posts no duplicate. Informs only — never holds, closes, or
    relabels. Exit 0 on a clean run, 20 on gh/transport failure."""
    api, issue = args.api, args.issue

    labels = api.issue_label_names(issue)
    if labels is None:
        print(f"validate: gh-failure stage=labels issue={issue}", file=sys.stderr)
        return EXIT_TRANSPORT
    if "kraken-task" not in labels:
        print(f"validate: #{issue} is not a kraken-task issue — no-op")
        return EXIT_OK

    body = api.issue_body(issue)
    if body is None:
        print(f"validate: gh-failure stage=body issue={issue}", file=sys.stderr)
        return EXIT_TRANSPORT

    missing = []
    if not any(lbl.startswith("project:") for lbl in labels):
        missing.append(VALIDATE_PROJECT_MISSING)
    if is_empty_section(section_body(body, "Goal")):
        missing.append(VALIDATE_GOAL_MISSING)
    if is_empty_section(section_body(body, "Acceptance")):
        missing.append(VALIDATE_ACCEPTANCE_MISSING)

    if not missing:
        print(f"validate: #{issue} is compliant — no-op")
        return EXIT_OK

    body_to_post = validation_body(missing)

    records = api.comment_records(issue)
    if records is None:
        print(f"validate: gh-failure stage=comments issue={issue}", file=sys.stderr)
        return EXIT_TRANSPORT
    prior = latest_validation_comment(records)
    # rstrip: a re-read body may pick up a trailing newline the transport adds;
    # our own posted body never carries one, so normalizing both is exact.
    if prior is not None and prior.rstrip("\n") == body_to_post.rstrip("\n"):
        print(f"validate: #{issue} already carries an identical validation comment — no-op")
        return EXIT_OK

    if not api.post_comment(issue, body_to_post):
        print(f"validate: gh-failure stage=comment issue={issue}", file=sys.stderr)
        return EXIT_TRANSPORT
    print(f"validate: #{issue} flagged (missing: project/Goal/Acceptance as listed)")
    return EXIT_OK


def is_identity_label(name: str) -> bool:
    """A label cleanup MUST preserve on a closed task: the task-type label
    (kraken-task) and its project routing label (project:<name>). Everything else
    — every state-machine label (in-progress / needs-decision / awaiting-merge)
    and any unrelated label — is stripped, so a closed issue reads clean and
    label-based queue filters never match dead state (PROTOCOL.md §10)."""
    return name == "kraken-task" or name.startswith("project:")


def cmd_cleanup(args: argparse.Namespace) -> int:
    """Strip every non-identity label off a CLOSED kraken-task issue except
    kraken-task itself and its project:<name> label (cleanup-closed.yml). Closing
    a task (the PR's `Closes` line, or a manual close) otherwise leaves whatever
    state-machine label it carried — awaiting-merge, needs-decision, even a stale
    in-progress — attached forever, so label-based filters keep matching dead
    state. A no-op when nothing but identity labels remain. The close event gates
    the workflow; this reads the issue's live labels and removes the rest, one at
    a time (idempotent — each removal targets a label the read just returned).
    Exit 0 on success, 20 on any gh/transport failure."""
    api, issue = args.api, args.issue

    labels = api.issue_label_names(issue)
    if labels is None:
        print(f"cleanup: gh-failure stage=labels issue={issue}", file=sys.stderr)
        return EXIT_TRANSPORT
    if "kraken-task" not in labels:
        print(f"cleanup: #{issue} is not a kraken-task issue — no-op")
        return EXIT_OK

    stripped = 0
    for name in labels:
        if is_identity_label(name):
            continue
        if not api.swap_labels(issue, remove=name):
            print(f"cleanup: gh-failure stage=remove issue={issue} label={name}",
                  file=sys.stderr)
            return EXIT_TRANSPORT
        stripped += 1

    # A closed task must not leave its lock behind: a crashed worker's lease
    # could linger past the close, and so could a generation a steal failed to
    # collect — drop every one of them. Idempotent: an already-absent ref counts
    # as deleted, and a task with no lease reads as an empty ladder.
    ok, refs = Refs(api).of(issue)
    if not ok or not Refs(api).drop(issue, [g for g, _s in refs]):
        print(f"cleanup: gh-failure stage=ref issue={issue}", file=sys.stderr)
        return EXIT_TRANSPORT

    print(f"cleanup: #{issue} done stripped={stripped}")
    return EXIT_OK
