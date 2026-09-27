"""Standing a coordination repo up, and its hygiene passes: validate, cleanup.

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
from .queue import missing_requirements
from .render import render_init
from .state import States

# --- subcommand: init --------------------------------------------------------

def bundled_asset(name: str) -> str:
    """The raw bytes of an asset shipped in this skill's folder."""
    with open(os.path.join(SKILL_DIR, name), "rb") as fh:
        return fh.read()


# (bundled filename, destination in the coordination repo, commit message).
INIT_ASSETS = (
    ("task-template.yml", ".github/ISSUE_TEMPLATE/task.yml",
     "chore: add kraken task template"),
)

# Assets earlier releases installed and `init` now deletes: a retired cron would
# keep mutating labels behind the workers' backs. The sentinel must appear in the
# file, so a hand-written file at the same path is never deleted.
OBSOLETE_ASSETS = (
    (".github/workflows/reclaim-stale.yml", b"for kraken. Installed by"),
    (".github/workflows/requeue-on-reply.yml", b"for kraken. Installed by"),
    (".github/workflows/cleanup-closed.yml", b"for kraken. Installed by"),
    (".github/workflows/validate-task.yml", b"for kraken. Installed by"),
    (".github/kraken.py", b"kraken.py \xe2\x80\x94 the bundled worker-side transitions"),
)

# (name, color, description): the home of §3's SHOULD colors. Descriptions name
# the state, never the delivery form — a delivery without a PR is legal (§8).
CANONICAL_LABELS = (
    ("kraken-task", "1D76DB", "A unit of work for a kraken worker — the queue"),
    ("in-progress", "FBCA04", "Claimed by a worker and being executed"),
    ("needs-decision", "D93F0B",
     "Blocked on your decision — answer, then remove the label to requeue"),
    ("awaiting-merge", "0E8A16",
     "Delivered — waiting for your review and merge"),
    ("priority:high", "B60205",
     "Claimed ahead of normal tasks — a scheduling preference, not a state"),
)
PROJECT_LABEL_COLOR = "5319E7"
PROJECT_LABEL_DESC = (
    "Canonical project identity — a worker's --project filters on this"
)


def refuse_foreign_owner(api) -> int | None:
    """None when the token's login owns the slug, else the exit code of a
    reported refusal. `POST /user/repos` ignores the slug's owner (#174)."""
    owner = api.repo.split("/", 1)[0] if "/" in api.repo else None
    login = api.authenticated_login()
    if login is None:
        print("init: gh-failure stage=identity (GET /user) — cannot tell who "
              f"{api.repo} would be created under", file=sys.stderr)
        return EXIT_TRANSPORT
    if owner is None or owner.lower() == login.lower():
        return None
    print(f"init: refusing to create {api.repo}: the token authenticates as "
          f"'{login}', not '{owner}', and GitHub would create {login}/"
          f"{api.repo.split('/', 1)[1]} instead. Switch to {owner} "
          "(`gh auth switch`), or create the repo there first and re-run init.",
          file=sys.stderr)
    return EXIT_USAGE


def cmd_init(args: argparse.Namespace) -> int:
    """Stand up (or repair, or migrate) a coordination repo: create it private,
    install assets create-only, prune retired ones, upsert the labels.
    Idempotent; touches no issues."""
    api = args.api
    report = {
        "repo": api.repo,
        "repo_status": "exists",
        "assets": [],
        "labels": [],
        "project": args.project or None,
    }
    for step in (_ensure_repo, _install_assets, _prune_assets, _upsert_labels):
        rc = step(api, report)
        if rc is not None:
            return rc
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render_init(report))
    return EXIT_OK


def _ensure_repo(api, report: dict) -> int | None:
    if api.repo_exists():
        return None
    refused = refuse_foreign_owner(api)
    if refused is not None:
        return refused
    if not api.repo_create_private():
        print(f"init: gh-failure stage=repo repo={api.repo}", file=sys.stderr)
        return EXIT_TRANSPORT
    report["repo_status"] = "created"
    return None


def _install_assets(api, report: dict) -> int | None:
    # Create-only: an existing file, hand edits included, is left alone.
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
    return None


def _prune_assets(api, report: dict) -> int | None:
    for dest, sentinel in OBSOLETE_ASSETS:
        current, sha = api.get_content_meta(dest)
        if current is None or sha is None or sentinel not in current:
            continue  # absent, unreadable, or not ours to delete
        if not api.delete_content(dest, sha,
                                  f"chore: remove retired kraken asset {dest}"):
            print(f"init: gh-failure stage=prune path={dest}", file=sys.stderr)
            return EXIT_TRANSPORT
        report["assets"].append({"path": dest, "status": "removed"})
    return None


def _upsert_labels(api, report: dict) -> int | None:
    labels = list(CANONICAL_LABELS)
    if report["project"]:
        labels.append((f"project:{report['project']}", PROJECT_LABEL_COLOR,
                       PROJECT_LABEL_DESC))
    for lname, color, desc in labels:
        if not api.label_upsert(lname, color, desc):
            print(f"init: gh-failure stage=label label={lname}", file=sys.stderr)
            return EXIT_TRANSPORT
        report["labels"].append(lname)
    return None


# What tags the validator's own comment, so a re-run can find it.
VALIDATION_MARKER = {"type": "validation"}

# One actionable item per missing requirement.
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
VALIDATE_MESSAGES = {
    "project label": VALIDATE_PROJECT_MISSING,
    "Goal": VALIDATE_GOAL_MISSING,
    "Acceptance": VALIDATE_ACCEPTANCE_MISSING,
}


def validation_body(missing: Sequence[str]) -> str:
    """The validator's one comment. It informs only."""
    return "\n\n".join([
        "> 🐙 **Kraken task validator** — this task isn't ready for a worker to pick up yet.",
        "Please fix the following so it can be claimed (this gate only informs; "
        "it never holds, closes, or relabels your task):\n" + "\n".join(missing),
        "Once fixed, this check clears itself — no action needed here.",
        make_marker(VALIDATION_MARKER),
    ])


def latest_validation_comment(records: Sequence[CommentRecord]) -> str | None:
    """The newest prior validation comment's body, or None."""
    latest = None
    for rec in records:  # server order: keep the newest match
        body = rec.get("body") or ""
        if any((parse_marker(l) or {}).get("type") == "validation"
               for l in body.split("\n")):
            latest = body
    return latest


def cmd_validate(args: argparse.Namespace) -> int:
    """Flag a queue entry missing its project label, Goal or Acceptance with
    one actionable comment; debounced, so an unchanged verdict posts nothing.
    Informs only: it never holds, closes or relabels."""
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

    missing = [VALIDATE_MESSAGES[requirement]
               for requirement in missing_requirements(labels, body)]

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

    rc = _refresh_anchor(api, issue)
    if rc != EXIT_OK:
        return rc
    print(f"validate: #{issue} flagged (missing: project/Goal/Acceptance as listed)")
    return EXIT_OK


def _refresh_anchor(api, issue) -> int:
    """Move the record's anchor past the comment just posted (§3.1), or §6
    would read the validator's own comment as an operator's reply and requeue
    a held task. A task with no record has no anchor to move."""
    states = States(api)
    record = states.of(issue)
    if record.unknown:
        print(f"validate: gh-failure stage=record issue={issue}", file=sys.stderr)
        return EXIT_TRANSPORT
    if not record.recorded:
        return EXIT_OK
    total = api.comment_count(issue)
    if total is None:
        print(f"validate: gh-failure stage=anchor issue={issue}", file=sys.stderr)
        return EXIT_TRANSPORT
    if not states.write(issue, record.re_anchored(total)):
        print(f"validate: gh-failure stage=record issue={issue}", file=sys.stderr)
        return EXIT_TRANSPORT
    return EXIT_OK


def is_identity_label(name: str) -> bool:
    """A label cleanup keeps on a closed task (§10): kraken-task and
    project:<name>."""
    return name == "kraken-task" or name.startswith("project:")


def cmd_cleanup(args: argparse.Namespace) -> int:
    """Strip every non-identity label and every claim ref off a closed task, so
    label filters never match dead state. Idempotent."""
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

    ok, refs = Refs(api).of(issue)
    if not ok or not Refs(api).drop(issue, [g for g, _s in refs]):
        print(f"cleanup: gh-failure stage=ref issue={issue}", file=sys.stderr)
        return EXIT_TRANSPORT

    print(f"cleanup: #{issue} done stripped={stripped}")
    return EXIT_OK
