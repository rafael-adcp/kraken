"""Argument parsing and dispatch.

Part of the kraken protocol package; see __init__.py."""
from __future__ import annotations

import argparse
import sys
from typing import Sequence

from .contract import EXIT_OK, PROTOCOL_VERSION
from .comments import MARKER_TYPES, disclaimer, task_trailer
from .transport import Api
from .lease import (
    LEASE_DEFAULT_TTL_SECONDS, lease_renew_seconds, lease_ttl_seconds
)
from .queue import cmd_list_startable
from .reconcile import RECONCILER_WORKER, cmd_reap
from .claim import (
    cmd_claim, cmd_claim_next, cmd_deliver, cmd_escalate, cmd_heartbeat,
    cmd_note, cmd_release, cmd_release_open
)
from .next_action import NEXT_ACTIONS, cmd_next_action
from .watch import cmd_watch
from .loop import DEFAULT_POLL_SECONDS, cmd_loop, split_agent_argv
from .status import cmd_status
from .workflow import cmd_cleanup, cmd_init, cmd_validate

# --- subcommand: contract ----------------------------------------------------
# The read side of single-sourcing: consumers fetch the disclaimer format and
# marker vocabulary from here instead of re-declaring the literals.
CONTRACT_FIELDS = {
    "disclaimer": lambda args: [disclaimer(args.worker)],
    "task-trailer": lambda args: [task_trailer(args.repo, args.issue, args.worker)],
    "marker-types": lambda args: list(MARKER_TYPES),
    # The next-action verdict vocabulary. The skill documents what an agent does
    # for each one, and the lint executes this rather than diffing prose — the
    # same rule marker-types follows.
    "next-actions": lambda args: list(NEXT_ACTIONS),
    "protocol-version": lambda args: [str(PROTOCOL_VERSION)],
    # The lease clock, in seconds. A skill or a doc that needs the number asks
    # for it rather than copying it, so raising the TTL raises the renewal
    # cadence everywhere in one edit.
    "lease-ttl": lambda args: [str(lease_ttl_seconds())],
    "lease-renew": lambda args: [str(lease_renew_seconds())],
}


def cmd_contract(args: argparse.Namespace) -> int:
    """Print an authoritative contract literal (no network) — the single source of
    truth for the disclaimer format and marker vocabulary, so a format change lands
    in one place."""
    for line in CONTRACT_FIELDS[args.field](args):
        print(line)
    return EXIT_OK


# --- CLI ---------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kraken.py",
        description="Bundled kraken worker-side queue transitions.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list-startable", help="startable candidates / queue snapshot")
    p.add_argument("repo")
    p.add_argument("project")
    p.add_argument("--snapshot", action="store_true",
                   help="emit every open task as <number>:startable|held")
    p.set_defaults(func=cmd_list_startable)

    p = sub.add_parser("claim", help="queued -> in-progress")
    p.add_argument("repo")
    p.add_argument("issue")
    p.add_argument("worker")
    p.set_defaults(func=cmd_claim)

    p = sub.add_parser(
        "claim-next",
        help="list + guard + claim the oldest startable candidate in one shot",
    )
    p.add_argument("repo")
    p.add_argument("project")
    p.add_argument("worker")
    p.add_argument("--json", action="store_true",
                   help="emit the won claim as a JSON object {issue,title,body}")
    p.set_defaults(func=cmd_claim_next)

    p = sub.add_parser(
        "next-action",
        help="the driver loop in one call: resume the task this worker holds, "
             "or claim the next one, and say what to run next (JSON envelope)",
    )
    p.add_argument("repo")
    p.add_argument("project")
    p.add_argument("worker")
    p.add_argument("--text", action="store_true",
                   help="render the envelope as human-readable lines instead "
                        "of JSON (the JSON is the machine contract)")
    p.set_defaults(func=cmd_next_action)

    p = sub.add_parser("heartbeat",
                       help="liveness: advance the claim ref to a fresh commit")
    p.add_argument("repo")
    p.add_argument("issue")
    p.add_argument("worker")
    p.add_argument("message")
    p.set_defaults(func=cmd_heartbeat)

    p = sub.add_parser("escalate", help="in-progress -> needs-decision")
    p.add_argument("repo")
    p.add_argument("issue")
    p.add_argument("worker")
    p.add_argument("question_file")
    p.set_defaults(func=cmd_escalate)

    p = sub.add_parser("deliver", help="in-progress -> awaiting-merge")
    p.add_argument("repo")
    p.add_argument("issue")
    p.add_argument("worker")
    p.add_argument("result_file")
    p.add_argument("pr_url", nargs="?", default="")
    p.set_defaults(func=cmd_deliver)

    p = sub.add_parser("release", help="in-progress -> queued (honest release)")
    p.add_argument("repo")
    p.add_argument("issue")
    p.add_argument("worker")
    p.add_argument("reason", nargs="?", default="")
    p.set_defaults(func=cmd_release)

    p = sub.add_parser(
        "release-open",
        help="release every claim still recorded on this machine — the "
             "release-on-exit fast path a lifecycle hook or `loop` takes when a "
             "drain died holding a lease (always exits 0)",
    )
    p.add_argument("--worker", default=None,
                   help="scope the sweep to this worker's own claim. A caller "
                        "that knows its identity MUST pass it: an unscoped "
                        "sweep frees a co-located worker's live claim")
    p.add_argument("--reason", default="",
                   help="reason recorded on the `released` marker")
    p.add_argument("--stamp-wake-retry", action="store_true",
                   help="stamp the wake-retry flag first, so an armed watcher "
                        "re-emits the wake the dead turn consumed")
    p.set_defaults(func=cmd_release_open)

    p = sub.add_parser(
        "note",
        help="post a free-form worker comment (disclaimer prepended, inert "
             "`note` marker); changes no label or claim ref",
    )
    p.add_argument("repo")
    p.add_argument("issue")
    p.add_argument("worker")
    p.add_argument("body_file")
    p.set_defaults(func=cmd_note)

    p = sub.add_parser("watch", help="poll the queue, print on a startable change")
    p.add_argument("repo")
    p.add_argument("project")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser(
        "loop",
        help="drive a drain from OUTSIDE the model: poll the queue and invoke "
             "an agent CLI only when a task is startable (an idle queue spends "
             "no tokens); release this worker's claim if the agent dies holding "
             "it. The agent command goes after `--` and must carry {prompt}",
        epilog="example: loop OWNER/tasks my_app env-1 -- copilot -p \"{prompt}\" "
               "--allow-all-tools --no-ask-user",
    )
    p.add_argument("repo")
    p.add_argument("project")
    p.add_argument("worker")
    p.add_argument("--once", action="store_true",
                   help="run one drain pass and exit instead of polling. The "
                        "agent's own exit status is passed through, so a cron "
                        "or CI caller can tell a failed drain from a clean one "
                        "— but it shares this program's namespace, so a "
                        "non-zero may equally be the loop itself (20 the queue "
                        "read, 13 an unknown project, 2 an unrunnable agent). "
                        "Treat any non-zero as 'this drain did not complete' "
                        "and read the kraken-loop: lines on stderr for which")
    p.add_argument("--poll", type=int, default=None,
                   help="poll cadence in seconds (default: "
                        "KRAKEN_LOOP_POLL_SECONDS env, else "
                        f"{DEFAULT_POLL_SECONDS})")
    p.add_argument("--prompt-file", default="",
                   help="file holding the drain instruction handed to the agent "
                        "(default: the built-in one-pass prompt)")
    p.set_defaults(func=cmd_loop)

    p = sub.add_parser(
        "reap",
        help="run the §6 reconcile stand-alone — reclaim repeatedly expired "
             "claims, delete orphan locks (a drain does this itself)",
    )
    p.add_argument("repo")
    p.add_argument("worker", nargs="?", default=RECONCILER_WORKER,
                   help="who the reclaim comments are attributed to "
                        f"(default: {RECONCILER_WORKER})")
    p.add_argument("--ttl", type=int, default=None,
                   help="lease TTL in seconds (default: KRAKEN_LEASE_TTL_SECONDS "
                        f"env, else {LEASE_DEFAULT_TTL_SECONDS})")
    p.set_defaults(func=cmd_reap)

    p = sub.add_parser(
        "validate",
        help="flag a task missing its project label, Goal, or Acceptance by "
             "commenting on it (debounced; informs only). `status` reports the "
             "same three checks read-only, for the whole queue at once",
    )
    p.add_argument("repo")
    p.add_argument("issue")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser(
        "cleanup",
        help="strip every state/non-identity label off a closed task, keeping "
             "only kraken-task and project:<name> (cosmetic; nothing decides on "
             "a closed task's labels)",
    )
    p.add_argument("repo")
    p.add_argument("issue")
    p.set_defaults(func=cmd_cleanup)

    p = sub.add_parser(
        "status",
        help="read-only operator console: review / decision / in-flight queues",
    )
    p.add_argument("repo")
    p.add_argument("--project", default="",
                   help="scope every queue to project:<name> (default: whole queue)")
    p.add_argument("--json", action="store_true",
                   help="emit the stable machine-readable status schema")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser(
        "init",
        help="stand up a coordination repo: private repo + bundled assets + "
             "canonical labels (idempotent; touches no issues)",
    )
    p.add_argument("repo")
    p.add_argument("--project", default="",
                   help="also upsert the project:<name> routing label")
    p.add_argument("--json", action="store_true",
                   help="emit the machine-readable init report")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser(
        "contract",
        help="print an authoritative contract literal (disclaimer / marker "
             "vocabulary) for consumers to derive from — no network",
    )
    p.add_argument("field", choices=sorted(CONTRACT_FIELDS),
                   help="which contract literal to print")
    p.add_argument("--worker", default="<worker-name>",
                   help="worker name to substitute into the disclaimer "
                        "(default: the doc placeholder <worker-name>)")
    p.add_argument("--repo", default="<coordination-repo>",
                   help="coordination repo slug for the task-trailer field "
                        "(default: the doc placeholder <coordination-repo>)")
    p.add_argument("--issue", default="<issue>",
                   help="task issue number for the task-trailer field "
                        "(default: the doc placeholder <issue>)")
    p.set_defaults(func=cmd_contract)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    # `<kraken args> -- <agent command>` is split before parsing, because
    # argparse cannot express it: REMAINDER would swallow this program's own
    # flags along with the tail. Only `loop` reads it; every other subcommand
    # gets an empty list it never looks at. See split_agent_argv.
    head, agent = split_agent_argv(
        sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(head)
    args.agent = agent
    # The client every subcommand talks through, built ONCE here and carried on
    # the parsed args. Constructing it inside each `cmd_*` would give a test no
    # way to hand the program a stand-in short of rebinding a module attribute,
    # which is the thing the object exists to stop.
    if not getattr(args, "api", None):
        args.api = Api(getattr(args, "repo", "") or "")
    return args.func(args)
