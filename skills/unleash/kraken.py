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
    3   claim-next only: no candidate was startable — the queue is empty, or
        every candidate turned out held/lost as it iterated (nothing to claim,
        an honest empty result, not a fault)
    2   bad invocation (missing file / unknown mode)
"""

import argparse
import datetime
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
# claim-next only: the queue held no startable candidate — either it was empty,
# or every candidate turned out held/lost as claim-next iterated. An honest
# "nothing to claim", distinct from success (0) and from a transport fault (20).
EXIT_NONE = 3
EXIT_USAGE = 2

# The three "held" labels: a task carrying any of them is claimed, escalated, or
# delivered — never startable, never re-claimable without a window reset.
HELD_LABELS = ("in-progress", "needs-decision", "awaiting-merge")

# --- protocol version --------------------------------------------------------
# The wire contract this program speaks (PROTOCOL.md). protocol/2 carries every
# machine payload in a structured hidden marker (see MARKER_* below); it still
# READS protocol/1's visible line grammar so pre-existing threads keep
# arbitrating, but every comment it WRITES is protocol/2 only.
PROTOCOL_VERSION = 2

# --- structured hidden markers (kraken-protocol/2) ---------------------------
# A state-changing comment carries its machine payload in ONE hidden HTML-comment
# marker — invisible in the rendered GitHub UI, so the visible prose is pure
# human courtesy — of the form:
#     <!-- kraken {"type":"claim","worker":"env-1"} -->
# The payload is compact one-line JSON with a required string "type". Encoding it
# with json.dumps (not string interpolation) is the whole point of protocol/2: it
# retires the CRLF/quoting/prefix-scan hazard class the visible line grammar
# inherited from the shell era. ensure_ascii keeps the marker pure ASCII (no
# astral-plane bytes) so the reaper's grep and the requeue filter never hit a
# locale-dependent match the way the 🐙 disclaimer does.
MARKER_PREFIX = "<!-- kraken "
MARKER_SUFFIX = " -->"
MARKER_RE = re.compile(r"<!--\s*kraken\s+(\{.*?\})\s*-->")

# The claim-window reset transition types: the most recent of these ends the
# current claim window, so a dead worker's claim (stale-claim), an honest
# hand-back (released), an escalation (needs-decision), or a delivered task
# bounced back by review (delivered) can all be re-claimed. Marker "type" values
# and the legacy prefixes below MUST agree, and each MUST appear in PROTOCOL.md —
# the lint enforces that.
RESET_TYPES = ("released", "stale-claim", "needs-decision", "delivered")

# The two liveness types: a comment carrying either proves the worker is alive,
# anchoring the reaper's staleness clock (PROTOCOL.md §6). Nothing else does.
LIVENESS_TYPES = ("claim", "heartbeat")

# Legacy (protocol/1) claim-window reset prefixes, still read so pre-existing
# threads arbitrate correctly during migration. Each maps to the RESET_TYPES
# value that drops its trailing colon (released: -> released, etc.). Every
# keyword here must appear in PROTOCOL.md's protocol/1 migration section — the
# lint enforces that.
WINDOW_RESET_PREFIXES = ("released:", "stale-claim:", "needs-decision:", "delivered:")

# Every marker "type" this program builds or arbitrates on — the protocol/2
# vocabulary. (`requeue` is operator-only; the workflow reads it, this program
# never emits it, so it is not here.) The lint checks each appears in
# PROTOCOL.md's marker table by executing `kraken.py contract marker-types`.
MARKER_TYPES = LIVENESS_TYPES + RESET_TYPES

# The protocol/1 legacy line-grammar prefixes this program still READS (dual-read
# migration, machine_event above): the claim line, the liveness line, and every
# window-reset line. Documented in PROTOCOL.md §4's migration section.
LEGACY_LINE_PREFIXES = ("claimed-by:", "heartbeat:") + WINDOW_RESET_PREFIXES


def make_marker(payload):
    """Render a machine payload dict as the protocol/2 hidden marker. Compact,
    ASCII-only JSON so the marker never carries a byte the reaper/requeue greps
    could miss under a C locale."""
    return MARKER_PREFIX + json.dumps(payload, separators=(",", ":")) + MARKER_SUFFIX


def parse_marker(line):
    """Decode the kraken marker payload on a line, or None if the line carries no
    well-formed marker. A marker with a non-dict body or no string "type" is
    treated as absent — a malformed marker never silently arbitrates."""
    m = MARKER_RE.search(line)
    if not m:
        return None
    try:
        obj = json.loads(m.group(1))
    except (ValueError, json.JSONDecodeError):
        return None
    if isinstance(obj, dict) and isinstance(obj.get("type"), str):
        return obj
    return None


def machine_event(line, *, reset_prefixes=WINDOW_RESET_PREFIXES):
    """Normalize one comment line to a {"type", ...} machine event, reading BOTH
    wire formats: a protocol/2 marker takes precedence, else the protocol/1
    visible line grammar. Returns None for a line that is neither. This single
    normalizer is what lets every consumer read the two formats identically."""
    line = line.rstrip("\r")
    marker = parse_marker(line)
    if marker is not None:
        return marker
    # protocol/1 fallback: the visible line grammar.
    if line.startswith("claimed-by:"):
        return {"type": "claim", "worker": line[len("claimed-by:"):].lstrip(" ")}
    for prefix in reset_prefixes:
        if line.startswith(prefix):
            return {"type": prefix[:-1]}
    if line.startswith("heartbeat:"):
        return {"type": "heartbeat", "worker": line[len("heartbeat:"):].lstrip(" ")}
    return None

# The attribution disclaimer — the ONE authoritative definition of its format.
# Every worker authenticates as the operator, so a worker comment reads exactly
# like a human's without this blockquote. It heads every comment a transition
# writes; a blank line must follow it or GitHub folds the body into the quote.
# {worker} is the only placeholder. Docs (SKILL.md, PROTOCOL.md §4) quote it
# illustratively and every other consumer (the requeue workflow filter, the test
# helpers, the skill lint) derives from or is verified against this constant via
# `kraken.py contract` — nothing re-declares the format by hand.
DISCLAIMER = "> 🐙 **Kraken worker `{worker}`** — automated comment from a Claude Code tentacle, not a human."


def disclaimer(worker):
    return DISCLAIMER.format(worker=worker)


def compose_comment(worker, prose, payload):
    """Assemble a protocol/2 state-changing comment: the attribution disclaimer,
    the human-facing prose (courtesy only — never machine-parsed), then the one
    hidden marker carrying the machine payload. Blank lines separate the three so
    GitHub does not fold the body into the disclaimer's blockquote."""
    parts = [disclaimer(worker)]
    prose = (prose or "").strip("\n")
    if prose:
        parts.append(prose)
    parts.append(make_marker(payload))
    return "\n\n".join(parts)


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
    # lines); markers (and legacy machine lines) sit on their own line, so a
    # flat line scan downstream is exactly what arbitration needs.
    return out.split("\n")


def comment_records(repo, issue):
    """Every comment as a {"body", "createdAt"} record, in server order —
    paginated past 100 through the SAME REST comments endpoint comment_bodies
    walks. `status` needs the timestamps (heartbeat-age anchoring) that the
    body-only `comment_bodies` drops; reading them off the identical paginated
    path means the age anchor is never computed from a truncated 100-comment
    history (the very bug comment_bodies exists to avoid for the claim window).

    Returns a list of dicts, or None on transport failure. `gh --jq` emits one
    compact JSON object per comment (interior newlines escaped), so a per-line
    decode is exact."""
    rc, out = run_gh([
        "api",
        f"repos/{repo}/issues/{issue}/comments",
        "--paginate",
        "--jq", ".[] | {body, createdAt}",
    ])
    if rc != 0:
        return None
    records = []
    decoder = json.JSONDecoder()
    idx = 0
    length = len(out)
    while idx < length:
        # Skip inter-object whitespace/newlines: `gh --jq` streams one object
        # per result, but a jq that pretty-prints spreads each across lines, so
        # decode object-by-object rather than line-by-line.
        while idx < length and out[idx] in " \t\r\n":
            idx += 1
        if idx >= length:
            break
        try:
            obj, end = decoder.raw_decode(out, idx)
        except (ValueError, json.JSONDecodeError):
            break
        if isinstance(obj, dict):
            records.append(obj)
        idx = end
    return records


# --- machine marker + legacy line grammar ------------------------------------

def arbitrate_winner(lines, *, reset_prefixes=WINDOW_RESET_PREFIXES):
    """The claim-window tiebreaker, isolated for testing.

    Scan each comment line in server order, reading BOTH wire formats through
    machine_event (a protocol/2 marker or the protocol/1 line grammar): any
    reset event clears the running winner (older claims no longer count), and the
    FIRST claim of the current window wins. Returns the winning worker name, or
    "" if the window holds no live claim."""
    winner = ""
    for raw in lines:
        event = machine_event(raw, reset_prefixes=reset_prefixes)
        if event is None:
            continue
        etype = event.get("type")
        if etype in RESET_TYPES:
            winner = ""
        elif etype == "claim":
            if not winner:
                winner = event.get("worker", "")
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


def classify_queue(repo, project, include_body=False):
    """The shared startable/held classification list-startable and watch's
    snapshot both read — one code path so the filter can't drift between them.
    Returns a list of (number, title, createdAt, "startable"|"held") sorted
    oldest-first, or None on transport failure. With include_body=True each row
    gains a fifth element, the issue body, so claim-next can brief a subagent
    from the win without a second fetch (the GraphQL walk already has it)."""
    nodes = fetch_open_tasks(repo)
    if nodes is None:
        return None
    project_label = f"project:{project}"
    nodes = [
        n for n in nodes
        if project_label in {l.get("name", "") for l in n.get("labels", {}).get("nodes", [])}
    ]
    nodes.sort(key=lambda n: n.get("createdAt", ""))

    rows = []            # [number, title, createdAt, state-or-None, body]
    fallback_targets = []  # (row_index, dep_number) needing the depends-on batch

    for node in nodes:
        number = node["number"]
        title = node.get("title", "")
        created = node.get("createdAt", "")
        body = node.get("body") or ""
        label_names = [l.get("name", "") for l in node.get("labels", {}).get("nodes", [])]
        if any(h in label_names for h in HELD_LABELS):
            rows.append([number, title, created, "held", body])
            continue

        blockers = node.get("blockedBy", {}).get("nodes", [])
        if blockers:
            blocked = any(str(b.get("state", "")).upper() == "OPEN" for b in blockers)
            rows.append([number, title, created, "held" if blocked else "startable", body])
            continue

        dep = None
        for line in body.split("\n"):
            m = re.match(r"^depends-on: *#([0-9]+)", line)
            if m:
                dep = int(m.group(1))
                break
        if dep is None:
            rows.append([number, title, created, "startable", body])
            continue
        rows.append([number, title, created, None, body])
        fallback_targets.append((len(rows) - 1, dep))

    if fallback_targets:
        dep_open = resolve_depends_on(repo, sorted({dep for _, dep in fallback_targets}))
        if dep_open is None:
            return None
        for idx, dep in fallback_targets:
            rows[idx][3] = "held" if dep_open.get(dep, False) else "startable"

    if include_body:
        return [tuple(r) for r in rows]
    return [(n, t, c, s) for n, t, c, s, _ in rows]


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

def _claim_once(repo, issue, worker):
    """The one contended claim sequence — guard, label, comment, arbitrate —
    executed identically every time (PROTOCOL.md §5). Returns an exit code and
    prints the same `claim:` diagnostic line for each outcome. Shared by the
    `claim` subcommand and by `claim-next`'s per-candidate loop so the two can
    never drift: a loss backs off writing nothing, exactly here, once."""

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

    # 2. Label, then 3. the claim comment (disclaimer, courtesy prose, marker).
    if not swap_labels(repo, issue, add="in-progress"):
        print(f"claim: gh-failure issue={issue} stage=label")
        return EXIT_TRANSPORT
    body = compose_comment(
        worker, "Claimed this task — starting work now.",
        {"type": "claim", "worker": worker},
    )
    if not post_comment(repo, issue, body):
        print(f"claim: gh-failure issue={issue} stage=comment")
        return EXIT_TRANSPORT

    # 4. Arbitrate — re-read the (fully paginated) comment history; the first
    #    claim of the current claim window wins.
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


def cmd_claim(args):
    return _claim_once(args.repo, args.issue, args.worker)


# --- subcommand: claim-next --------------------------------------------------

def cmd_claim_next(args):
    """Collapse the deterministic claim loop into one invocation: list startable
    candidates oldest-first, then guard/label/comment/arbitrate each in turn,
    stopping at the first win. Per-candidate losses (10/11) move on to the next
    one, exactly as the drain loop did by hand; a transport fault (20) stops
    immediately with the state-unknown semantics; an exhausted queue is an
    honest EXIT_NONE. Never turns a lost tiebreaker into a retry on the same
    issue (PROTOCOL.md §5) — it iterates forward, never back."""
    repo, project, worker = args.repo, args.project, args.worker

    rows = classify_queue(repo, project, include_body=True)
    if rows is None:
        print("claim-next: gh-failure stage=list")
        return EXIT_TRANSPORT

    for number, title, _created, state, body in rows:  # already oldest-first
        if state != "startable":
            continue
        rc = _claim_once(repo, number, worker)
        if rc == EXIT_OK:
            if args.json:
                print(json.dumps({"issue": number, "title": title, "body": body}))
            else:
                print(f"claim-next: claimed issue={number} worker={worker}")
                print(f"{number}\t{title}")
                print()
                print(body)
            return EXIT_OK
        if rc == EXIT_TRANSPORT:
            # State is now ambiguous — do NOT move on to another candidate while
            # a write of ours may have half-landed. Re-check before any retry.
            print(f"claim-next: gh-failure issue={number} — state unknown, re-check")
            return EXIT_TRANSPORT
        # EXIT_LOST (10) / EXIT_NOT_CLEAR (11): back off, try the next candidate.

    print(f"claim-next: none project:{project}")
    return EXIT_NONE


# --- subcommand: heartbeat ---------------------------------------------------

def cmd_heartbeat(args):
    repo, issue, worker, message = args.repo, args.issue, args.worker, args.message
    body = compose_comment(worker, message, {"type": "heartbeat", "worker": worker})
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

    body = compose_comment(
        worker, read_body_file(question_file),
        {"type": "needs-decision", "worker": worker},
    )
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

    payload = {"type": "delivered", "worker": worker}
    prose = read_body_file(result_file)
    if pr_url:
        payload["pr"] = pr_url
        prose = f"{prose}\n\nPR: {pr_url}"
    body = compose_comment(worker, prose, payload)
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
    payload = {"type": "released", "worker": worker}
    prose = "Released this claim — the task rejoins the queue."
    if reason:
        payload["reason"] = reason
        prose = f"{prose}\n\nReason: {reason}"
    body = compose_comment(worker, prose, payload)
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


# --- subcommand: status ------------------------------------------------------
#
# The operator console, mechanized (PROTOCOL.md §12): the same read-only view
# skills/status/SKILL.md used to teach an LLM to orchestrate — review queue,
# decision queue, in-flight with heartbeat ages, the merged-PR-but-open-issue
# orphan heuristic, and the launch recon — computed deterministically here so
# the skill is a thin renderer and the data is reusable (scripts, cron, `--json`
# for downstream tooling). Read-only: no label change, no comment, no write.
#
# Reuse, don't duplicate: the queue itself comes from the same paginated
# fetch_open_tasks GraphQL walk list-startable uses (held tasks keep their
# kraken-task label, so the walk returns them). Per-issue comment reads happen
# only for tasks that are actually in flight or awaiting merge — an idle queue
# stays O(pages) — and go through the paginated comment_records path so the
# heartbeat anchor never reads a truncated >100-comment history.

_PR_URL_RE = re.compile(r"https?://\S+?/pull/\d+")


def project_names_of(node):
    """The project:<name> suffixes carried by a queue node's labels."""
    names = set()
    for lbl in node.get("labels", {}).get("nodes", []):
        name = lbl.get("name", "")
        if name.startswith("project:"):
            names.add(name[len("project:"):])
    return names


def label_names_of(node):
    return {lbl.get("name", "") for lbl in node.get("labels", {}).get("nodes", [])}


def parse_iso(ts):
    """An ISO-8601 UTC timestamp (…Z) to epoch seconds, or None if unparseable."""
    if not ts:
        return None
    try:
        dt = datetime.datetime.strptime(ts.strip(), "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return dt.replace(tzinfo=datetime.timezone.utc).timestamp()


def _is_machine_line(line):
    """Whether a line carries a worker-liveness machine payload — a protocol/2
    claim/heartbeat marker or the protocol/1 ^claimed-by:/^heartbeat: line. Reset
    lines (delivered:/released:/…) are not liveness: they already drop
    in-progress, so they never anchor a still-held claim (PROTOCOL.md §6)."""
    event = machine_event(line)
    return event is not None and event.get("type") in LIVENESS_TYPES


def heartbeat_anchor(records):
    """The createdAt of the newest comment carrying a worker liveness machine
    line (^claimed-by: or ^heartbeat:), or None when the worker never spoke.

    This is the reaper's staleness anchor (reclaim-stale.yml), computed the same
    way: only those two lines prove liveness, and an operator comment — carrying
    neither — never resets the clock, so a human poking a dead worker's issue
    cannot make it look alive. None means no anchor exists at all (a malformed
    or silent claim), which the reaper treats as infinitely stale."""
    newest = None
    for rec in records:
        body = rec.get("body") or ""
        if any(_is_machine_line(l) for l in body.split("\n")):
            created = rec.get("createdAt") or ""
            if created and (newest is None or created > newest):
                newest = created
    return newest


def flat_comment_lines(records):
    """Every comment body flattened to a per-line list, in server order — the
    shape arbitrate_winner reads."""
    lines = []
    for rec in records:
        lines.extend((rec.get("body") or "").split("\n"))
    return lines


def parse_pr_url(records):
    """The delivery PR URL for an awaiting-merge task: the newest structured
    source wins — a protocol/2 delivered marker's "pr" field or the protocol/1
    `pr:` line — falling back to the newest GitHub pull-request URL
    anywhere in the thread. None when no PR was recorded."""
    from_marker = None
    fallback = None
    for rec in records:  # server order — keep overwriting so the newest wins
        for raw in (rec.get("body") or "").split("\n"):
            marker = parse_marker(raw)
            if marker is not None and marker.get("pr"):
                from_marker = marker["pr"]
            else:
                line = raw.rstrip("\r").strip()
                if line.startswith("pr:"):
                    url = line[len("pr:"):].strip()
                    if url:
                        from_marker = url
            m = _PR_URL_RE.search(raw)
            if m:
                fallback = m.group(0)
    return from_marker or fallback


def pr_is_merged(pr_url):
    """Whether a delivery PR is already merged — the orphan heuristic's only
    signal. Returns True/False, or None on transport failure (a flag is never
    guessed from a failed read)."""
    data = gh_json(["pr", "view", pr_url, "--json", "state,mergedAt"])
    if data is None:
        return None
    return bool(data.get("mergedAt")) or str(data.get("state", "")).upper() == "MERGED"


def list_projects(repo):
    """Every project:<name> label configured in the repo, sorted, prefix
    stripped — the launch recon points a worker at each. Read from `gh label
    list` (not the open-task walk) so a project with no open task still gets a
    launch line. Returns a sorted name list, or None on transport failure."""
    data = gh_json(["-R", repo, "label", "list", "--limit", "200", "--json", "name"])
    if data is None:
        return None
    return sorted(
        n["name"][len("project:"):]
        for n in data
        if str(n.get("name", "")).startswith("project:")
    )


def format_age(seconds):
    """A compact human age: '42s', '12m', '3h', '4d'. 'unknown' when there is no
    anchor (a worker that never left a liveness marker)."""
    if seconds is None:
        return "unknown"
    seconds = int(seconds)
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h"
    return f"{hours // 24}d"


def compute_status(repo, project, nodes, now, *, comment_reader, pr_merged,
                   project_lister):
    """Pure-ish status computation, transport injected so it is unit-testable:
    given the queue nodes (from fetch_open_tasks) and reader callbacks, build the
    review/decision/in-flight/projects report. Returns the report dict, or None
    on any injected-transport failure (propagated as exit 20)."""
    if project:
        pl = project
        nodes = [n for n in nodes if pl in project_names_of(n)]

    review, decision, in_flight = [], [], []
    seen_projects = set()

    for node in sorted(nodes, key=lambda n: (n.get("createdAt", ""), n.get("number", 0))):
        seen_projects |= project_names_of(node)
        number = node["number"]
        title = node.get("title", "")
        labels = label_names_of(node)

        if "awaiting-merge" in labels:
            records = comment_reader(repo, number)
            if records is None:
                return None
            pr_url = parse_pr_url(records)
            orphan = False
            if pr_url:
                merged = pr_merged(pr_url)
                if merged is None:
                    return None
                orphan = bool(merged)
            review.append({"number": number, "title": title,
                           "pr_url": pr_url, "orphan": orphan})
        elif "needs-decision" in labels:
            decision.append({"number": number, "title": title})
        elif "in-progress" in labels:
            records = comment_reader(repo, number)
            if records is None:
                return None
            worker = arbitrate_winner(flat_comment_lines(records)) or None
            anchor = heartbeat_anchor(records)
            age = None
            if anchor:
                anchor_epoch = parse_iso(anchor)
                if anchor_epoch is not None:
                    age = max(0, int(now - anchor_epoch))
            in_flight.append({"number": number, "title": title, "worker": worker,
                              "heartbeat_anchor": anchor,
                              "heartbeat_age_seconds": age})

    if project:
        projects = [project]
    else:
        projects = project_lister(repo)
        if projects is None:
            return None

    return {
        "repo": repo,
        "project": project or None,
        "generated_at": datetime.datetime.fromtimestamp(
            now, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "review_queue": review,
        "decision_queue": decision,
        "in_flight": in_flight,
        "orphans": [r["number"] for r in review if r["orphan"]],
        "projects": projects,
    }


def render_status(report):
    """The human console — the shape skills/status/SKILL.md documents. A thin
    renderer over compute_status's report; empty groups say so plainly."""
    repo = report["repo"]
    project = report["project"]
    scope = f"project:{project} @ {repo}" if project else f"@ {repo}"
    lines = [f"🐙 kraken status — {scope}", ""]

    review = report["review_queue"]
    lines.append(f"  📋 Review queue (awaiting-merge) — "
                 f"{len(review) or 'nothing'} waiting for your merge"
                 if review else
                 "  📋 Review queue (awaiting-merge) — nothing waiting")
    for item in review:
        link = f" → {item['pr_url']}" if item["pr_url"] else " → (no PR link recorded)"
        flag = "  ⚠️  PR looks merged — close it?" if item["orphan"] else ""
        lines.append(f"     #{item['number']}  {item['title']}{link}{flag}")
    lines.append("")

    decision = report["decision_queue"]
    lines.append(f"  ❓ Decision queue (needs-decision) — "
                 f"{len(decision)} waiting for your call"
                 if decision else
                 "  ❓ Decision queue (needs-decision) — nothing waiting")
    for item in decision:
        lines.append(f"     #{item['number']}  {item['title']}  (options in thread)")
    lines.append("")

    in_flight = report["in_flight"]
    lines.append(f"  ⚙️  In flight (in-progress) — {len(in_flight)} running"
                 if in_flight else
                 "  ⚙️  In flight (in-progress) — nothing running")
    for item in in_flight:
        worker = item["worker"] or "unknown"
        age = format_age(item["heartbeat_age_seconds"])
        lines.append(f"     #{item['number']}  {item['title']}  · worker {worker} "
                     f"· last heartbeat {age} ago")
    lines.append("")

    orphans = report["orphans"]
    if orphans:
        joined = ", ".join(f"#{n}" for n in orphans)
        lines.append(f"  ⚠️  {len(orphans)} possible orphan(s): {joined} — "
                     f"PR looks merged but the issue is still open. You decide.")
        lines.append("")

    if project is None:
        projects = report["projects"]
        if projects:
            lines.append("  🚀 Launch — one worker per prepared environment")
            for name in projects:
                lines.append(f"     /kraken:unleash {repo} "
                             f"--worker-name <worker-name> --project {name}")
        else:
            lines.append("  🚀 Launch — no project: labels yet "
                         "(create one with init --project)")
    return "\n".join(lines)


def cmd_status(args):
    repo, project = args.repo, args.project
    nodes = fetch_open_tasks(repo)
    if nodes is None:
        print("status: gh-failure stage=list", file=sys.stderr)
        return EXIT_TRANSPORT

    report = compute_status(
        repo, project, nodes, time.time(),
        comment_reader=comment_records,
        pr_merged=pr_is_merged,
        project_lister=list_projects,
    )
    if report is None:
        print("status: gh-failure stage=read", file=sys.stderr)
        return EXIT_TRANSPORT

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render_status(report))
    return EXIT_OK


# The contract literals `kraken.py contract` exposes, each a list of lines. This
# is the read side of single-sourcing: the disclaimer format and the machine
# marker / legacy-line / claim-window vocabulary live in the constants above,
# and every other consumer derives from or is verified against them by executing
# this command rather than re-declaring the literals.
CONTRACT_FIELDS = {
    "disclaimer": lambda args: [disclaimer(args.worker)],
    "reset-prefixes": lambda args: list(WINDOW_RESET_PREFIXES),
    "reset-types": lambda args: list(RESET_TYPES),
    "liveness-types": lambda args: list(LIVENESS_TYPES),
    "marker-types": lambda args: list(MARKER_TYPES),
    "legacy-line-prefixes": lambda args: list(LEGACY_LINE_PREFIXES),
}


def cmd_contract(args):
    """Print an authoritative contract literal (no network). The single source of
    truth for the disclaimer format and the machine-line/marker vocabulary — the
    requeue workflow filter, the test helpers, and the skill lint read these
    instead of re-declaring them, so a format change lands in exactly one place."""
    for line in CONTRACT_FIELDS[args.field](args):
        print(line)
    return EXIT_OK


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
        "contract",
        help="print an authoritative contract literal (disclaimer / machine-line "
             "vocabulary) for consumers to derive from — no network",
    )
    p.add_argument("field", choices=sorted(CONTRACT_FIELDS),
                   help="which contract literal to print")
    p.add_argument("--worker", default="<worker-name>",
                   help="worker name to substitute into the disclaimer "
                        "(default: the doc placeholder <worker-name>)")
    p.set_defaults(func=cmd_contract)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
