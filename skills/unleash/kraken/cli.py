"""Argument parsing and dispatch.

Part of the kraken protocol package; see __init__.py."""
from __future__ import annotations

import argparse
import dataclasses
from typing import Callable, Sequence

from .contract import EXIT_OK, PROTOCOL_VERSION
from .comments import MARKER_TYPES, disclaimer, task_trailer
from .transport import Api
from .lease import (
    LEASE_DEFAULT_TTL_SECONDS, lease_renew_seconds, lease_ttl_seconds
)
from .queue import cmd_list_startable
from .reconcile import RECONCILER_WORKER, cmd_reap
from .claim import (
    cmd_claim, cmd_claim_next, cmd_heartbeat, cmd_note
)
from .terminal import (
    cmd_deliver, cmd_escalate, cmd_release
)
from .next_action import NEXT_ACTIONS, cmd_next_action
from .watch import cmd_watch
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


@dataclasses.dataclass(frozen=True)
class Command:
    """One subcommand as data: the name it is invoked by, the function that runs
    it, its `--help` line, and its arguments.

    `args` is the positional signature — names in order, a trailing `?` marking
    one that may be omitted (defaulting to ""). `opts` holds everything needing
    explicit argparse keywords, passed through verbatim and added after `args`;
    argparse reads a name without dashes as a positional, so the two positionals
    with keywords of their own (`reap worker`, `contract field`) ride there too."""

    name: str
    func: Callable[[argparse.Namespace], int]
    help: str
    args: str = ""
    opts: tuple[tuple[str, dict], ...] = ()


COMMANDS = (
    Command("list-startable", cmd_list_startable,
            "startable candidates / queue snapshot",
            "repo project",
            (("--snapshot", dict(
                action="store_true",
                help="emit every open task as <number>:startable|held")),)),

    Command("claim", cmd_claim,
            "queued -> in-progress",
            "repo issue worker"),

    Command("claim-next", cmd_claim_next,
            "list + guard + claim the oldest startable candidate in one shot",
            "repo project worker",
            (("--json", dict(
                action="store_true",
                help="emit the won claim as a JSON object {issue,title,body}")),)),

    Command("next-action", cmd_next_action,
            "the driver loop in one call: resume the task this worker holds, "
            "or claim the next one, and say what to run next (JSON envelope)",
            "repo project worker",
            (("--text", dict(
                action="store_true",
                help="render the envelope as human-readable lines instead "
                     "of JSON (the JSON is the machine contract)")),)),

    Command("heartbeat", cmd_heartbeat,
            "liveness: advance the claim ref to a fresh commit",
            "repo issue worker message"),

    Command("escalate", cmd_escalate,
            "in-progress -> needs-decision",
            "repo issue worker question_file"),

    Command("deliver", cmd_deliver,
            "in-progress -> awaiting-merge",
            "repo issue worker result_file pr_url?"),

    Command("release", cmd_release,
            "in-progress -> queued (honest release)",
            "repo issue worker reason?"),

    Command("note", cmd_note,
            "post a free-form worker comment (disclaimer prepended, inert "
            "`note` marker); changes no label or claim ref",
            "repo issue worker body_file"),

    Command("watch", cmd_watch,
            "poll the queue, print on a startable change",
            "repo project"),

    Command("reap", cmd_reap,
            "run the §6 reconcile stand-alone — reclaim repeatedly expired "
            "claims, delete orphan locks (a drain does this itself)",
            "repo",
            (("worker", dict(
                nargs="?", default=RECONCILER_WORKER,
                help="who the reclaim comments are attributed to "
                     f"(default: {RECONCILER_WORKER})")),
             ("--ttl", dict(
                 type=int, default=None,
                 help="lease TTL in seconds (default: KRAKEN_LEASE_TTL_SECONDS "
                      f"env, else {LEASE_DEFAULT_TTL_SECONDS})")))),

    Command("validate", cmd_validate,
            "flag a task missing its project label, Goal, or Acceptance by "
            "commenting on it (debounced; informs only). `status` reports the "
            "same three checks read-only, for the whole queue at once",
            "repo issue"),

    Command("cleanup", cmd_cleanup,
            "strip every state/non-identity label off a closed task, keeping "
            "only kraken-task and project:<name> (cosmetic; nothing decides on "
            "a closed task's labels)",
            "repo issue"),

    Command("status", cmd_status,
            "read-only operator console: review / decision / in-flight queues",
            "repo",
            (("--project", dict(
                default="",
                help="scope every queue to project:<name> (default: whole queue)")),
             ("--json", dict(
                 action="store_true",
                 help="emit the stable machine-readable status schema")))),

    Command("init", cmd_init,
            "stand up a coordination repo: private repo + bundled assets + "
            "canonical labels (idempotent; touches no issues)",
            "repo",
            (("--project", dict(
                default="",
                help="also upsert the project:<name> routing label")),
             ("--json", dict(
                 action="store_true",
                 help="emit the machine-readable init report")))),

    Command("contract", cmd_contract,
            "print an authoritative contract literal (disclaimer / marker "
            "vocabulary) for consumers to derive from — no network",
            "",
            (("field", dict(
                choices=sorted(CONTRACT_FIELDS),
                help="which contract literal to print")),
             ("--worker", dict(
                 default="<worker-name>",
                 help="worker name to substitute into the disclaimer "
                      "(default: the doc placeholder <worker-name>)")),
             ("--repo", dict(
                 default="<coordination-repo>",
                 help="coordination repo slug for the task-trailer field "
                      "(default: the doc placeholder <coordination-repo>)")),
             ("--issue", dict(
                 default="<issue>",
                 help="task issue number for the task-trailer field "
                      "(default: the doc placeholder <issue>)")))),
)


def build_parser() -> argparse.ArgumentParser:
    """The parser, walked off `COMMANDS` — one subparser per entry."""
    parser = argparse.ArgumentParser(
        prog="kraken.py",
        description="Bundled kraken worker-side queue transitions.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for cmd in COMMANDS:
        p = sub.add_parser(cmd.name, help=cmd.help)
        for name in cmd.args.split():
            if name.endswith("?"):
                p.add_argument(name[:-1], nargs="?", default="")
            else:
                p.add_argument(name)
        for name, kwargs in cmd.opts:
            p.add_argument(name, **kwargs)
        p.set_defaults(func=cmd.func)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # The client every subcommand talks through, built ONCE here and carried on
    # the parsed args. Constructing it inside each `cmd_*` would give a test no
    # way to hand the program a stand-in short of rebinding a module attribute,
    # which is the thing the object exists to stop.
    if not getattr(args, "api", None):
        args.api = Api(getattr(args, "repo", "") or "")
    return args.func(args)
