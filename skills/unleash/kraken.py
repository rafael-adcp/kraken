#!/usr/bin/env python3
"""kraken.py — the bundled worker-side transitions, one stdlib-only program.

This consolidates the seven bundled transition scripts
(`list-startable`, `claim`, `heartbeat`, `escalate`, `deliver`, `release`,
`watch`) that used to be separate `.sh` files into a single program with
subcommands. The thin `*.sh` files next to this module now just `exec` into it,
so every existing caller (the unleash skill, the conformance suite, the
SessionEnd hook) keeps working unchanged.

Why Python: the shell versions carried a running commentary of CRLF, `printf`,
and quoting hazards they had to defend against by hand. Moving to Python kills
that whole class of bug, makes the claim-window arbitration unit-testable in
isolation, and lets pagination (both the queue listing and the >100-comment
claim window) be handled once, correctly.

Transport (phase 1): `gh` stays the transport. Every GitHub call shells out to
`gh api` / `gh issue`, exactly like the scripts did, so the conformance stub
(which intercepts `gh` on PATH) and the operator's existing auth keep working
for free. A direct-REST phase 2 is possible later but out of scope here.
list-startable's queue fetch is the one exception to "shell out per call": it
batches labels, native blocked-by, and body into a single paginated
`gh api graphql` walk (classify_queue/fetch_open_tasks below), so an idle
watch poll costs O(pages), not one REST call per non-held task.

Exit-code contract (PROTOCOL.md §12), preserved verbatim from the scripts:
    0   success
    10  lost the claim tiebreaker — back off, pick the next candidate
    11  no longer clear — a held label appeared since listing; skip the task
    20  gh / network transport failure — state unknown, re-check before retry
    2   bad invocation (missing file / unknown mode)
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time

# Exit codes — the agent branches on these; keep them identical to the scripts.
EXIT_OK = 0
EXIT_LOST = 10
EXIT_NOT_CLEAR = 11
EXIT_TRANSPORT = 20
EXIT_USAGE = 2

# The three "held" labels: a task carrying any of them is claimed, escalated, or
# delivered — never startable, never re-claimable without a window reset.
HELD_LABELS = ("in-progress", "needs-decision", "awaiting-merge")

# Claim-window reset prefixes: a claimed-by: line older than the most recent of
# these no longer counts, so a dead worker's claim (stale-claim:), an honest
# hand-back (released:), an escalation (needs-decision:), or a delivered task
# bounced back by review (delivered:) can all be re-claimed. Every keyword here
# must appear as a machine line in PROTOCOL.md — the lint enforces that.
WINDOW_RESET_PREFIXES = ("released:", "stale-claim:", "needs-decision:", "delivered:")

# The attribution disclaimer. Every worker authenticates as the operator, so a
# worker comment reads exactly like a human's without this blockquote. It heads
# every comment a transition writes; a blank line must follow it or GitHub folds
# the body into the quote. Kept byte-identical to SKILL.md / PROTOCOL.md (the
# lint asserts that), with {worker} as the only placeholder.
DISCLAIMER = "> 🐙 **Kraken worker `{worker}`** — automated comment from a Claude Code tentacle, not a human."


def disclaimer(worker):
    return DISCLAIMER.format(worker=worker)


# --- transport ---------------------------------------------------------------

def run_gh(args):
    """Run `gh <args>`; return (returncode, stdout). Never raises on non-zero —
    the callers map a non-zero return to the exit-20 transport-failure path
    themselves, exactly where the scripts did `|| exit 20`."""
    try:
        proc = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except FileNotFoundError:
        return 127, ""
    return proc.returncode, proc.stdout


def gh_json(args):
    """Run a `gh` call expected to emit JSON on stdout. Returns the parsed
    object, or None on any transport / decode failure (mapped to exit 20)."""
    rc, out = run_gh(args)
    if rc != 0:
        return None
    try:
        return json.loads(out)
    except (ValueError, json.JSONDecodeError):
        return None


def graphql(query):
    """Run a `gh api graphql` call; return the parsed {"data": ...} envelope,
    or None on transport / decode failure."""
    return gh_json(["api", "graphql", "-f", f"query={query}"])


def comment_bodies(repo, issue):
    """Every comment body on the issue, in server order — paginated past 100.

    `gh issue view --json comments` silently caps at 100 comments (it does not
    page the nested GraphQL connection), so a long-lived task's claim window
    could scroll out of view and re-arbitration would read a truncated history.
    The REST comments endpoint with `--paginate` walks every page, so the claim
    window is always evaluated against the complete comment history.

    Returns a list of body strings, or None on transport failure."""
    rc, out = run_gh([
        "api",
        f"repos/{repo}/issues/{issue}/comments",
        "--paginate",
        "--jq", ".[].body",
    ])
    if rc != 0:
        return None
    # `--jq .[].body` prints one body per line (multi-line bodies span several
    # lines); machine lines are one-per-line, so a flat line scan downstream is
    # exactly what arbitration needs.
    return out.split("\n")


# --- machine-line grammar ----------------------------------------------------

def arbitrate_winner(lines, *, reset_prefixes=WINDOW_RESET_PREFIXES):
    """The claim-window tiebreaker, isolated for testing.

    Scan machine lines in server order: any reset line clears the running
    winner (older claimed-by: lines no longer count), and the FIRST claimed-by:
    of the current window wins. Returns the winning worker name, or "" if the
    window holds no live claim."""
    winner = ""
    for raw in lines:
        line = raw.rstrip("\r")
        if any(line.startswith(p) for p in reset_prefixes):
            winner = ""
        elif line.startswith("claimed-by:"):
            if not winner:
                winner = line[len("claimed-by:"):].lstrip(" ")
    return winner


# --- claim state file --------------------------------------------------------

def state_dir():
    return os.environ.get("KRAKEN_STATE_DIR") or os.path.join(
        os.path.expanduser("~"), ".kraken"
    )


def claim_state_path(worker):
    return os.path.join(state_dir(), f"claim-{worker}.json")


def write_claim_state(repo, issue, worker):
    """Record the open claim so the SessionEnd hook can auto-release it if the
    worker's session ends before a terminal transition. Best-effort: a state dir
    we cannot write is never worth failing a won claim over — the reaper backs
    us up regardless."""
    d = state_dir()
    try:
        os.makedirs(d, exist_ok=True)
        with open(claim_state_path(worker), "w", encoding="utf-8") as fh:
            json.dump({"repo": repo, "issue": str(issue), "worker": worker}, fh)
            fh.write("\n")
    except OSError:
        pass


def clear_claim_state(worker):
    """Drop the claim state file on a terminal transition (deliver / escalate /
    release), so a later graceful exit does not re-release a claim we no longer
    hold. Best-effort."""
    try:
        os.remove(claim_state_path(worker))
    except OSError:
        pass


# --- comment composition -----------------------------------------------------

def post_comment(repo, issue, body):
    rc, _ = run_gh(["-R", repo, "issue", "comment", str(issue), "--body", body])
    return rc == 0


def swap_labels(repo, issue, remove=None, add=None):
    args = ["-R", repo, "issue", "edit", str(issue)]
    if remove:
        args += ["--remove-label", remove]
    if add:
        args += ["--add-label", add]
    rc, _ = run_gh(args)
    return rc == 0


# --- subcommand: list-startable ---------------------------------------------
#
# The queue fetch and the blocked-by check are both batched through GraphQL so
# an idle poll costs a small, queue-size-independent number of round trips —
# never the old one-REST-call-per-non-held-task.
#
# GitHub's GraphQL `issues(labels: [...])` argument is a UNION over multiple
# values (confirmed against a live repo — unlike the REST `--label` flag's
# AND), so passing kraken-task and project:<name> there would OR the two
# label sets together instead of intersecting them. Filtering on the single
# "kraken-task" label server-side and the project label client-side (against
# the very labels this same call already returns) sidesteps the ambiguity.

def fetch_open_tasks(repo):
    """Every OPEN kraken-task issue in the repo, across all projects — number,
    title, createdAt, body, labels, and native blocked-by, all in one paginated
    GraphQL walk. Returns the node list, or None on transport failure."""
    owner, name = repo.split("/", 1)
    nodes = []
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
        resp = graphql(query)
        if resp is None:
            return None
        page = resp["data"]["repository"]["issues"]
        nodes.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            return nodes
        cursor = page["pageInfo"]["endCursor"]


def resolve_depends_on(repo, targets):
    """Resolve every `depends-on: #N` fallback target's open/closed state in
    one batched GraphQL call (one aliased `iN: issue(number: N) { state }`
    field per distinct target), never one call per candidate. Returns
    {number: is_open}, or None on transport failure."""
    if not targets:
        return {}
    owner, name = repo.split("/", 1)
    fields = " ".join(f"i{n}: issue(number: {n}) {{ state }}" for n in targets)
    resp = graphql(f'{{ repository(owner: "{owner}", name: "{name}") {{ {fields} }} }}')
    if resp is None:
        return None
    repo_obj = resp["data"]["repository"]
    return {
        n: str((repo_obj.get(f"i{n}") or {}).get("state", "")).upper() == "OPEN"
        for n in targets
    }


def classify_queue(repo, project):
    """The shared startable/held classification list-startable and watch's
    snapshot both read — one code path so the filter can't drift between them.
    Returns a list of (number, title, createdAt, "startable"|"held") sorted
    oldest-first, or None on transport failure."""
    nodes = fetch_open_tasks(repo)
    if nodes is None:
        return None
    project_label = f"project:{project}"
    nodes = [
        n for n in nodes
        if project_label in {l.get("name", "") for l in n.get("labels", {}).get("nodes", [])}
    ]
    nodes.sort(key=lambda n: n.get("createdAt", ""))

    rows = []            # [number, title, createdAt, state-or-None]
    fallback_targets = []  # (row_index, dep_number) needing the depends-on batch

    for node in nodes:
        number = node["number"]
        title = node.get("title", "")
        created = node.get("createdAt", "")
        label_names = [l.get("name", "") for l in node.get("labels", {}).get("nodes", [])]
        if any(h in label_names for h in HELD_LABELS):
            rows.append([number, title, created, "held"])
            continue

        blockers = node.get("blockedBy", {}).get("nodes", [])
        if blockers:
            blocked = any(str(b.get("state", "")).upper() == "OPEN" for b in blockers)
            rows.append([number, title, created, "held" if blocked else "startable"])
            continue

        dep = None
        for line in (node.get("body") or "").split("\n"):
            m = re.match(r"^depends-on: *#([0-9]+)", line)
            if m:
                dep = int(m.group(1))
                break
        if dep is None:
            rows.append([number, title, created, "startable"])
            continue
        rows.append([number, title, created, None])
        fallback_targets.append((len(rows) - 1, dep))

    if fallback_targets:
        dep_open = resolve_depends_on(repo, sorted({dep for _, dep in fallback_targets}))
        if dep_open is None:
            return None
        for idx, dep in fallback_targets:
            rows[idx][3] = "held" if dep_open.get(dep, False) else "startable"

    return [tuple(r) for r in rows]


def cmd_list_startable(args):
    rows = classify_queue(args.repo, args.project)
    if rows is None:
        return EXIT_TRANSPORT

    if args.snapshot:
        for number, _, _, state in sorted(rows, key=lambda r: r[0]):
            print(f"{number}:{state}")
    else:
        for number, title, _, state in rows:  # already createdAt-sorted
            if state == "startable":
                print(f"{number}\t{title}")
    return EXIT_OK


# --- subcommand: claim -------------------------------------------------------

def cmd_claim(args):
    repo, issue, worker = args.repo, args.issue, args.worker

    # 1. Guard — re-fetch labels; a held task is skipped with zero writes.
    labels_obj = gh_json(["-R", repo, "issue", "view", str(issue), "--json", "labels"])
    if labels_obj is None:
        print(f"claim: gh-failure issue={issue} stage=guard")
        return EXIT_TRANSPORT
    label_names = [lbl.get("name", "") for lbl in labels_obj.get("labels", [])]
    for held in HELD_LABELS:
        if held in label_names:
            print(f"claim: held issue={issue} label={held}")
            return EXIT_NOT_CLEAR

    # 2. Label, then 3. the claim comment (disclaimer, blank line, machine line).
    if not swap_labels(repo, issue, add="in-progress"):
        print(f"claim: gh-failure issue={issue} stage=label")
        return EXIT_TRANSPORT
    body = f"{disclaimer(worker)}\n\nclaimed-by: {worker}"
    if not post_comment(repo, issue, body):
        print(f"claim: gh-failure issue={issue} stage=comment")
        return EXIT_TRANSPORT

    # 4. Arbitrate — re-read the (fully paginated) comment history; the first
    #    claimed-by: of the current claim window wins.
    bodies = comment_bodies(repo, issue)
    if bodies is None:
        print(f"claim: gh-failure issue={issue} stage=arbitrate")
        return EXIT_TRANSPORT

    winner = arbitrate_winner(bodies)
    if winner == worker:
        write_claim_state(repo, issue, worker)
        print(f"claim: claimed issue={issue} worker={worker}")
        return EXIT_OK
    print(f"claim: lost-tiebreaker issue={issue} winner={winner or 'unknown'}")
    return EXIT_LOST


# --- subcommand: heartbeat ---------------------------------------------------

def cmd_heartbeat(args):
    repo, issue, worker, message = args.repo, args.issue, args.worker, args.message
    body = f"{disclaimer(worker)}\n\nheartbeat: {worker}\n\n{message}"
    if not post_comment(repo, issue, body):
        print(f"heartbeat: gh-failure issue={issue}")
        return EXIT_TRANSPORT
    print(f"heartbeat: posted issue={issue} worker={worker}")
    return EXIT_OK


# --- subcommand: escalate ----------------------------------------------------

def read_body_file(path):
    """Read a file the way `$(cat file)` did: content with trailing newlines
    stripped (interior preserved)."""
    with open(path, encoding="utf-8") as fh:
        return fh.read().rstrip("\n")


def cmd_escalate(args):
    repo, issue, worker, question_file = args.repo, args.issue, args.worker, args.question_file
    if not os.path.isfile(question_file):
        print(f"escalate: no such file {question_file}", file=sys.stderr)
        return EXIT_USAGE

    body = f"{disclaimer(worker)}\n\nneeds-decision: {worker}\n\n{read_body_file(question_file)}"
    if not post_comment(repo, issue, body):
        print(f"escalate: gh-failure issue={issue} stage=comment")
        return EXIT_TRANSPORT
    if not swap_labels(repo, issue, remove="in-progress", add="needs-decision"):
        print(f"escalate: gh-failure issue={issue} stage=labels")
        return EXIT_TRANSPORT

    clear_claim_state(worker)
    print(f"escalate: escalated issue={issue} worker={worker}")
    return EXIT_OK


# --- subcommand: deliver -----------------------------------------------------

def cmd_deliver(args):
    repo, issue, worker, result_file = args.repo, args.issue, args.worker, args.result_file
    pr_url = args.pr_url
    if not os.path.isfile(result_file):
        print(f"deliver: no such file {result_file}", file=sys.stderr)
        return EXIT_USAGE

    machine = f"delivered: {worker}"
    if pr_url:
        machine += f"\npr: {pr_url}"
    body = f"{disclaimer(worker)}\n\n{machine}\n\n{read_body_file(result_file)}"
    if not post_comment(repo, issue, body):
        print(f"deliver: gh-failure issue={issue} stage=comment")
        return EXIT_TRANSPORT
    if not swap_labels(repo, issue, remove="in-progress", add="awaiting-merge"):
        print(f"deliver: gh-failure issue={issue} stage=labels")
        return EXIT_TRANSPORT

    clear_claim_state(worker)
    suffix = f" pr={pr_url}" if pr_url else ""
    print(f"deliver: delivered issue={issue} worker={worker}{suffix}")
    return EXIT_OK


# --- subcommand: release -----------------------------------------------------

def cmd_release(args):
    repo, issue, worker, reason = args.repo, args.issue, args.worker, args.reason
    body = f"{disclaimer(worker)}\n\nreleased: {worker}"
    if reason:
        body += f"\nreason: {reason}"
    if not post_comment(repo, issue, body):
        print(f"release: gh-failure issue={issue} stage=comment")
        return EXIT_TRANSPORT
    if not swap_labels(repo, issue, remove="in-progress"):
        print(f"release: gh-failure issue={issue} stage=label")
        return EXIT_TRANSPORT

    clear_claim_state(worker)
    print(f"release: released issue={issue} worker={worker}")
    return EXIT_OK


# --- subcommand: watch -------------------------------------------------------

def snapshot_state(repo, project):
    """Compute the queue snapshot in-process — the same startable/held split
    list-startable emits in --snapshot mode, via the same classify_queue.
    Returns the snapshot text, or None on a transport failure (the watcher
    skips that cycle)."""
    rows = classify_queue(repo, project)
    if rows is None:
        return None
    return "\n".join(
        f"{n}:{state}" for n, _, _, state in sorted(rows, key=lambda r: r[0])
    )


def cmd_watch(args):
    repo, project = args.repo, args.project
    poll_seconds = int(os.environ.get("KRAKEN_WATCH_POLL_SECONDS", "60"))

    prev = None
    while True:
        snapshot = snapshot_state(repo, project)
        if snapshot is not None:
            startable = [
                line for line in snapshot.split("\n") if line.endswith(":startable")
            ]
            count = len(startable)
            # The whole emit gate: a startable task exists AND the queue changed
            # since the last poll. No re-emission timer, nothing else.
            if count > 0 and snapshot != prev:
                numbers = " ".join(
                    "#" + line.split(":", 1)[0] for line in startable
                )
                print(
                    f"kraken-queue: {count} startable task(s) "
                    f"in project:{project} ({numbers})",
                    flush=True,
                )
            prev = snapshot
        time.sleep(poll_seconds)


# --- CLI ---------------------------------------------------------------------

def build_parser():
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

    p = sub.add_parser("heartbeat", help="liveness comment")
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

    p = sub.add_parser("watch", help="poll the queue, print on a startable change")
    p.add_argument("repo")
    p.add_argument("project")
    p.set_defaults(func=cmd_watch)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
