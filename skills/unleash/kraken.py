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
import base64
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
# The wire contract this program speaks (PROTOCOL.md). protocol/3 carries every
# machine payload in a structured hidden marker (see MARKER_* below) and reads
# ONLY markers: the retired protocol/1 visible line grammar is no longer parsed,
# so free text in a comment can never occupy a machine-line position.
PROTOCOL_VERSION = 3

# --- plugin version ----------------------------------------------------------
# The installed plugin's version, as stamped in the bundled marketplace manifest
# the release workflow bumps. It is the `kraken@<version>` in the Kraken-Task
# commit trailer (task_trailer below): read at runtime, never a second literal to
# drift, and never something the worker model has to guess.
PLUGIN_MANIFEST = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "..", ".claude-plugin", "plugin.json",
)
PLUGIN_VERSION_UNKNOWN = "unknown"

# This module's own folder — where the bundled assets `init` installs
# (task-template.yml, this vendored kraken.py, and the four coordination
# workflows) ship, next to this file, exactly as the unleash skill resolves its
# bundled kraken.py.
SKILL_DIR = os.path.dirname(os.path.abspath(__file__))


def plugin_version(manifest=PLUGIN_MANIFEST):
    """The installed plugin's version, read from the bundled
    `.claude-plugin/plugin.json` two levels up from this module
    (skills/unleash/kraken.py -> <plugin root>/.claude-plugin/plugin.json). The
    release workflow bumps ONLY that file, so reading it at runtime keeps the
    Kraken-Task trailer's `kraken@<version>` in lockstep with the plugin without
    a second version literal to maintain. Returns ``"unknown"`` if the manifest
    is missing or unreadable — the trailer still forms, just unstamped."""
    try:
        with open(manifest, encoding="utf-8") as f:
            version = json.load(f).get("version")
    except (OSError, ValueError):
        return PLUGIN_VERSION_UNKNOWN
    return version if isinstance(version, str) and version else PLUGIN_VERSION_UNKNOWN

# --- structured hidden markers (kraken-protocol/3) ---------------------------
# A state-changing comment carries its machine payload in ONE hidden HTML-comment
# marker — invisible in the rendered GitHub UI, so the visible prose is pure
# human courtesy — of the form:
#     <!-- kraken {"type":"claim","worker":"env-1"} -->
# The payload is compact one-line JSON with a required string "type". Encoding it
# with json.dumps (not string interpolation) is the whole point of protocol/3: it
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
# bounced back by review (delivered) can all be re-claimed. Each MUST appear in
# PROTOCOL.md's marker table — the lint enforces that.
RESET_TYPES = ("released", "stale-claim", "needs-decision", "delivered")

# The two liveness types: a comment carrying either proves the worker is alive,
# anchoring the reaper's staleness clock (PROTOCOL.md §6). Nothing else does.
LIVENESS_TYPES = ("claim", "heartbeat")

# Every marker "type" this program builds or arbitrates on — the protocol/3
# vocabulary. (`requeue` is operator-only; the workflow reads it, this program
# never emits it, so it is not here.) The lint checks each appears in
# PROTOCOL.md's marker table by executing `kraken.py contract marker-types`.
MARKER_TYPES = LIVENESS_TYPES + RESET_TYPES


def make_marker(payload):
    """Render a machine payload dict as the protocol/3 hidden marker. Compact,
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


def machine_event(line):
    """Normalize one comment line to a {"type", ...} machine event by decoding its
    protocol/3 hidden marker, or None for a line carrying no well-formed marker.
    ONLY the marker is read: the retired protocol/1 visible line grammar is not
    parsed, so free text can never masquerade as a machine line. This single
    normalizer is what every consumer reads through."""
    return parse_marker(line.rstrip("\r"))

# The attribution disclaimer — the ONE authoritative definition of its format.
# Every worker authenticates as the operator, so a worker comment reads exactly
# like a human's without this blockquote. It heads every comment a transition
# writes; a blank line must follow it or GitHub folds the body into the quote.
# {worker} is the only placeholder. The line is deliberately **agent-agnostic** —
# it names no implementation ("a kraken tentacle", not "a Claude Code tentacle") —
# so every conforming worker sharing this kraken.py, whatever agent drives it,
# emits the identical disclaimer and the timeline reads uniformly. Docs (SKILL.md,
# PROTOCOL.md §4) quote it illustratively and every other consumer (the requeue
# workflow filter, the test helpers, the skill lint) derives from or is verified
# against this constant via `kraken.py contract` — nothing re-declares it by hand.
DISCLAIMER = "> 🐙 **Kraken worker `{worker}`** — automated comment from a kraken tentacle, not a human."


def disclaimer(worker):
    return DISCLAIMER.format(worker=worker)


# The Kraken-Task commit trailer — the ONE authoritative definition of its format,
# the delivery-side twin of DISCLAIMER. Every delivered commit carries it so a
# merge maps back to the task and the plugin version that produced it
# (PROTOCOL.md §11). `{version}` is sourced from the bundled plugin.json via
# plugin_version(), never hand-copied: the worker model cannot know the installed
# version, and a pasted literal is exactly the drift this single-sourcing kills.
# The companion `Co-Authored-By` line stays the agent's own — it carries the
# agent's identity, which this program cannot know — so only the kraken-specific
# line lives here.
TASK_TRAILER = "Kraken-Task: {repo}#{issue} (worker: {worker}, kraken@{version})"


def task_trailer(repo, issue, worker):
    """Compose the authoritative `Kraken-Task:` commit trailer, stamping the live
    plugin version so `kraken@<version>` is never guessed."""
    return TASK_TRAILER.format(
        repo=repo, issue=issue, worker=worker, version=plugin_version()
    )



def compose_comment(worker, prose, payload):
    """Assemble a protocol/3 state-changing comment: the attribution disclaimer,
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
    # lines); markers sit on their own line, so a flat line scan downstream is
    # exactly what arbitration needs.
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


# --- machine marker arbitration ----------------------------------------------

def arbitrate_winner(lines):
    """The claim-window tiebreaker, isolated for testing.

    Scan each comment line in server order, decoding each protocol/3 hidden
    marker through machine_event: any reset event clears the running winner
    (older claims no longer count), and the FIRST claim of the current window
    wins. Free-text prose carries no marker, so it is inert here. Returns the
    winning worker name, or "" if the window holds no live claim."""
    winner = ""
    for raw in lines:
        event = machine_event(raw)
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


def open_claim(worker):
    """Return the issue number (as a string) of an open claim this worker still
    holds, read from its claim-<worker>.json state file, or None when no open
    claim exists. The file's *presence* is the signal that a claim is
    unresolved: every terminal transition (deliver / escalate / release) removes
    it, so a resolved claim leaves nothing behind. A missing, unreadable, or
    malformed file is treated as no open claim — the guard it feeds must never
    fail a claim over an unparseable scratch file (the reaper backs us up)."""
    try:
        with open(claim_state_path(worker), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    issue = data.get("issue")
    return None if issue is None else str(issue)


def refuse_second_claim(worker, issue=None):
    """PROTOCOL.md §5: a worker MUST work one task at a time and MUST NOT claim a
    second task while it holds a claim. If a claim-<worker>.json state file marks
    an open claim, refuse — writing nothing — and return EXIT_NOT_CLEAR; return
    None when the worker is clear to claim.

    A recorded claim on the *same* `issue` is a permitted re-claim, not a second
    task: it is exactly the §5 network-failure caveat ("or while a claim of its
    own is in an unknown state after a network failure — re-check first"), so a
    retry of the ambiguous claim is allowed. `issue=None` (claim-next, always
    taking a *new* task) refuses on any open claim."""
    held = open_claim(worker)
    if held is None or (issue is not None and held == str(issue)):
        return None
    print(
        f"claim: refused worker={worker} holds={held} — one task at a time "
        f"(PROTOCOL.md §5); resolve the open claim first "
        f"(deliver / escalate / release)"
    )
    return EXIT_NOT_CLEAR


def wake_retry_flag_path():
    return os.path.join(state_dir(), "wake-retry")


def wake_retry_mtime():
    """mtime of the wake-retry flag the StopFailure hook stamps when a usage
    limit kills a turn on this machine (hooks/stop-failure-release.sh), or None
    when no flag exists. The watcher compares it against its own last emission
    to decide whether a wake it spent was consumed by a dead turn."""
    try:
        return os.path.getmtime(wake_retry_flag_path())
    except OSError:
        return None


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


def issue_label_names(repo, issue):
    """The label names currently on an issue, live. Returns a list, or None on
    transport failure — the coordination workflows read labels off the live
    issue (a labeled/edited event may have just changed them)."""
    obj = gh_json(["-R", repo, "issue", "view", str(issue), "--json", "labels"])
    if obj is None:
        return None
    return [lbl.get("name", "") for lbl in obj.get("labels", [])]


def issue_body(repo, issue):
    """The issue's body text, live. Returns a string ("" when the body is empty
    or null), or None on transport failure."""
    obj = gh_json(["-R", repo, "issue", "view", str(issue), "--json", "body"])
    if obj is None:
        return None
    return obj.get("body") or ""


def open_issue_numbers(repo, label):
    """Every OPEN issue number carrying `label`, as a list of ints (empty when
    none), or None on transport failure. The reaper's few in-progress issues."""
    rc, out = run_gh([
        "-R", repo, "issue", "list", "--label", label, "--state", "open",
        "--json", "number", "--jq", ".[].number",
    ])
    if rc != 0:
        return None
    nums = []
    for line in out.split("\n"):
        line = line.strip()
        if line:
            try:
                nums.append(int(line))
            except ValueError:
                pass
    return nums


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
    refused = refuse_second_claim(args.worker, args.issue)
    if refused is not None:
        return refused
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

    refused = refuse_second_claim(worker)
    if refused is not None:
        return refused

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


def wake_retry_due(flag_mtime, last_emit, retry_seconds, now):
    """Whether the watcher owes a lost-wake retry: the StopFailure hook stamped
    the wake-retry flag AFTER this watcher's last emission — meaning the turn
    that wake started died on a usage limit, so the wake was consumed for
    nothing — and the retry spacing has elapsed. A flag older than the last
    emission is stale (that wake's turn survived) and never re-triggers; no
    flag means no failed turn on record. This is NOT the removed blind
    re-emission timer: without a proven-dead wake, the change-gated emit stays
    the only path."""
    if flag_mtime is None:
        return False
    return flag_mtime > last_emit and now - last_emit >= retry_seconds


def cmd_watch(args):
    repo, project = args.repo, args.project
    poll_seconds = int(os.environ.get("KRAKEN_WATCH_POLL_SECONDS", "60"))
    retry_seconds = int(os.environ.get("KRAKEN_WATCH_RETRY_SECONDS", "300"))

    prev = None
    # Start at "now": retries are owed only for wakes THIS watcher emitted, so
    # a stale flag from an earlier session never triggers one.
    last_emit = time.time()
    while True:
        snapshot = snapshot_state(repo, project)
        if snapshot is not None:
            startable = [
                line for line in snapshot.split("\n") if line.endswith(":startable")
            ]
            count = len(startable)
            # The emit gate: a startable task exists AND either the queue
            # changed since the last poll, or a lost-wake retry is due
            # (wake_retry_due) — the one case where an unchanged queue still
            # hides an undelivered wake. No blind re-emission timer.
            due = wake_retry_due(
                wake_retry_mtime(), last_emit, retry_seconds, time.time()
            )
            if count > 0 and (snapshot != prev or due):
                numbers = " ".join(
                    "#" + line.split(":", 1)[0] for line in startable
                )
                print(
                    f"kraken-queue: {count} startable task(s) "
                    f"in project:{project} ({numbers})",
                    flush=True,
                )
                last_emit = time.time()
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
    """Whether a line carries a worker-liveness machine payload — a protocol/3
    claim/heartbeat marker. Reset markers (delivered/released/…) are not
    liveness: they already drop in-progress, so they never anchor a still-held
    claim (PROTOCOL.md §6)."""
    event = machine_event(line)
    return event is not None and event.get("type") in LIVENESS_TYPES


def heartbeat_anchor(records):
    """The createdAt of the newest comment carrying a worker liveness marker
    (a claim/heartbeat marker), or None when the worker never spoke.

    This is the reaper's staleness anchor (reclaim-stale.yml), computed the same
    way: only those two markers prove liveness, and an operator comment —
    carrying neither — never resets the clock, so a human poking a dead worker's
    issue cannot make it look alive. None means no anchor exists at all (a
    malformed or silent claim), which the reaper treats as infinitely stale."""
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
    source wins — a protocol/3 delivered marker's "pr" field — falling back to
    the newest GitHub pull-request URL anywhere in the thread. None when no PR
    was recorded."""
    from_marker = None
    fallback = None
    for rec in records:  # server order — keep overwriting so the newest wins
        for raw in (rec.get("body") or "").split("\n"):
            marker = parse_marker(raw)
            if marker is not None and marker.get("pr"):
                from_marker = marker["pr"]
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


# --- subcommand: init --------------------------------------------------------
# The bootstrap `init` mechanizes, single-sourced here so the skill and the
# program never disagree on the asset set or the label canon (issue #30, the
# trio with claim-next #25 and status #27).

# Each bundled asset init commits: (bundled filename next to this module,
# destination path in the coordination repo, commit message used on create).
INIT_ASSETS = (
    ("task-template.yml", ".github/ISSUE_TEMPLATE/task.yml",
     "chore: add kraken task template"),
    ("kraken.py", ".github/kraken.py",
     "chore: add kraken transition program"),
    ("reclaim-stale.yml", ".github/workflows/reclaim-stale.yml",
     "chore: add kraken reaper workflow"),
    ("cleanup-closed.yml", ".github/workflows/cleanup-closed.yml",
     "chore: add kraken cleanup-closed workflow"),
    ("requeue-on-reply.yml", ".github/workflows/requeue-on-reply.yml",
     "chore: add kraken requeue-on-reply workflow"),
    ("validate-task.yml", ".github/workflows/validate-task.yml",
     "chore: add kraken validate-task workflow"),
)

# The canonical state-machine labels — (name, color, description). The GitHub
# labels UI IS kraken's dashboard, so the colors trace the flow left to right:
# blue queued -> yellow working -> red needs-you / green ready-to-land. This is
# the one authoritative home for PROTOCOL.md §3's SHOULD colors; init upserts
# with --force so a re-run re-canonicalizes any drift in place.
CANONICAL_LABELS = (
    ("kraken-task", "1D76DB", "A unit of work for a kraken worker — the queue"),
    ("in-progress", "FBCA04", "Claimed by a worker and being executed"),
    ("needs-decision", "D93F0B",
     "Blocked on your decision — answer, then remove the label to requeue"),
    ("awaiting-merge", "0E8A16",
     "Delivered as a draft PR — waiting for your review and merge"),
)
PROJECT_LABEL_COLOR = "5319E7"
PROJECT_LABEL_DESC = (
    "Canonical project identity — a worker's --project filters on this"
)


def gh_repo_exists(repo):
    """True iff the coordination repo already exists (a clean `repo view`)."""
    rc, _ = run_gh(
        ["repo", "view", repo, "--json", "nameWithOwner", "--jq", ".nameWithOwner"]
    )
    return rc == 0


def gh_repo_create_private(repo):
    """Create the coordination repo PRIVATE — never public: the queue is
    instructions that run in a worker's environment with its credentials."""
    rc, _ = run_gh(["repo", "create", repo, "--private"])
    return rc == 0


def gh_get_content(repo, path):
    """The file's current bytes on the repo via the contents API, or None when it
    is absent (404) OR unreadable. The caller cannot tell 404 from a transport
    fault here and, like the skill, treats 'no readable file' as absent and
    creates — a PUT over a file that truly exists then fails server-side (no
    sha), so a lost read never silently clobbers a customized file."""
    rc, out = run_gh(["api", f"/repos/{repo}/contents/{path}", "--jq", ".content"])
    if rc != 0:
        return None
    try:
        return base64.b64decode(re.sub(r"\s+", "", out))
    except ValueError:
        return None


def gh_put_content(repo, path, data, message):
    """Create `path` on the repo with `data` via the contents API (create-only;
    callers reach here only when gh_get_content reported the file absent)."""
    b64 = base64.b64encode(data).decode("ascii")
    rc, _ = run_gh([
        "api", f"/repos/{repo}/contents/{path}", "-X", "PUT",
        "-f", f"message={message}", "-f", f"content={b64}",
    ])
    return rc == 0


def gh_label_upsert(repo, name, color, description):
    """Upsert a label with its canonical color/description via `--force` — a
    no-op create on a fresh repo, an in-place re-canonicalize on a re-run."""
    rc, _ = run_gh([
        "-R", repo, "label", "create", name, "--force",
        "--color", color, "--description", description,
    ])
    return rc == 0


def cmd_init(args):
    """Stand up a coordination repo: verify-or-create it private, install the
    bundled assets (create / skip-unchanged / flag-customized), and upsert the
    canonical labels. Idempotent — a second run creates nothing new. Touches no
    issues. Exit 0 on success, 20 on any gh/transport failure."""
    repo, project = args.repo, args.project
    report = {
        "repo": repo,
        "repo_status": "exists",
        "assets": [],
        "labels": [],
        "project": project or None,
    }

    # 1. Verify or create the repo (private).
    if not gh_repo_exists(repo):
        if not gh_repo_create_private(repo):
            print(f"init: gh-failure stage=repo repo={repo}", file=sys.stderr)
            return EXIT_TRANSPORT
        report["repo_status"] = "created"

    # 2. Install the bundled assets, create-only — never clobber a customized one.
    for name, dest, message in INIT_ASSETS:
        try:
            with open(os.path.join(SKILL_DIR, name), "rb") as fh:
                bundled = fh.read()
        except OSError:
            print(f"init: missing bundled asset {name}", file=sys.stderr)
            return EXIT_USAGE
        current = gh_get_content(repo, dest)
        if current is None:
            if not gh_put_content(repo, dest, bundled, message):
                print(f"init: gh-failure stage=asset path={dest}", file=sys.stderr)
                return EXIT_TRANSPORT
            status = "created"
        elif current == bundled:
            status = "unchanged"
        else:
            status = "customized"
        report["assets"].append({"path": dest, "status": status})

    # 3. Upsert the canonical labels (+ the project label when scoped).
    labels = list(CANONICAL_LABELS)
    if project:
        labels.append((f"project:{project}", PROJECT_LABEL_COLOR, PROJECT_LABEL_DESC))
    for lname, color, desc in labels:
        if not gh_label_upsert(repo, lname, color, desc):
            print(f"init: gh-failure stage=label label={lname}", file=sys.stderr)
            return EXIT_TRANSPORT
        report["labels"].append(lname)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render_init(report))
    return EXIT_OK


def render_init(report):
    """Human-facing init report: one line per repo/asset/label decision, then a
    summary line the skill can echo verbatim."""
    lines = [f"init: repo {report['repo']} ({report['repo_status']})"]
    for asset in report["assets"]:
        lines.append(f"init: asset {asset['path']} ({asset['status']})")
    for name in report["labels"]:
        lines.append(f"init: label {name} (upserted)")
    created = sum(1 for a in report["assets"] if a["status"] == "created")
    unchanged = sum(1 for a in report["assets"] if a["status"] == "unchanged")
    customized = sum(1 for a in report["assets"] if a["status"] == "customized")
    lines.append(
        f"init: done repo={report['repo']} repo_status={report['repo_status']} "
        f"assets_created={created} assets_unchanged={unchanged} "
        f"assets_customized={customized} labels={len(report['labels'])}"
    )
    return "\n".join(lines)



# --- coordination-repo workflow subcommands ----------------------------------
# The three logic-bearing coordination workflows (reclaim-stale, requeue-on-reply,
# validate-task) used to re-implement the protocol parse in a weaker language
# each — jq-regex, grep-ERE, awk. Vendored into the coordination repo (init
# installs this file as a sixth asset), kraken.py runs their logic directly, so
# there is ONE parser, in ONE language, sharing the same marker decoder,
# disclaimer, and label vocabulary the worker side already uses — and gaining the
# same unit tests. Each workflow shrinks to a checkout + a single exec of the
# matching subcommand below.

# The reaper's default staleness threshold, in hours. An in-progress issue whose
# worker's last liveness marker (claim/heartbeat) is older than this — or that
# carries no liveness marker at all — is reclaimed to needs-decision. The
# workflow passes it through the MAX_HOURS env var (kept for continuity).
REAP_DEFAULT_MAX_HOURS = 6


def stale_claim_body(reason):
    """The reaper's reclaim comment: human prose plus the protocol/3 stale-claim
    marker (a reset event — the most recent one ends the claim window so a fresh
    worker can re-claim). It carries NO attribution disclaimer: the reaper is not
    a worker but the coordination repo's own automation, authored server-side by
    the Actions bot (user.type == Bot), which is exactly how requeue-on-reply's
    Bot gate tells it apart from an operator comment."""
    marker = make_marker({"type": "stale-claim", "reason": reason})
    prose = (
        f"The worker has gone silent ({reason}) and likely died. To requeue, "
        "remove the needs-decision label; or investigate first."
    )
    return f"{prose}\n\n{marker}"


def cmd_reap(args):
    """Reclaim stale claims (reclaim-stale.yml). For every OPEN in-progress
    issue, anchor staleness to the worker's newest liveness marker
    (claim/heartbeat) — NOT the issue's updatedAt, so an operator poking a dead
    worker's issue never resets the clock — and when that anchor is older than
    MAX_HOURS (or absent entirely: infinitely stale), swap in-progress for
    needs-decision and post the stale-claim comment for the operator to triage.
    Exit 0 on success, 20 on any gh/transport failure."""
    repo = args.repo
    max_hours = args.max_hours
    if max_hours is None:
        try:
            max_hours = int(os.environ.get("MAX_HOURS", REAP_DEFAULT_MAX_HOURS))
        except ValueError:
            max_hours = REAP_DEFAULT_MAX_HOURS
    now = time.time()

    numbers = open_issue_numbers(repo, "in-progress")
    if numbers is None:
        print("reap: gh-failure stage=list", file=sys.stderr)
        return EXIT_TRANSPORT

    reclaimed = 0
    for num in numbers:
        # The whole comment history, paginated past 100 — the newest liveness
        # marker can sit past the first page on a long-lived task, and a capped
        # read would make a live, heartbeating worker look silent (see
        # comment_records / comment_bodies).
        records = comment_records(repo, num)
        if records is None:
            print(f"reap: gh-failure stage=comments issue={num}", file=sys.stderr)
            return EXIT_TRANSPORT

        anchor = heartbeat_anchor(records)
        if anchor is None:
            reason = "no worker heartbeat on record"  # infinitely stale
        else:
            anchor_epoch = parse_iso(anchor)
            if anchor_epoch is None:
                reason = "no worker heartbeat on record"
            else:
                age_hours = int((now - anchor_epoch) // 3600)
                if age_hours < max_hours:
                    continue
                reason = f"no worker heartbeat for {age_hours}h"

        if not swap_labels(repo, num, remove="in-progress", add="needs-decision"):
            print(f"reap: gh-failure stage=labels issue={num}", file=sys.stderr)
            return EXIT_TRANSPORT
        if not post_comment(repo, num, stale_claim_body(reason)):
            print(f"reap: gh-failure stage=comment issue={num}", file=sys.stderr)
            return EXIT_TRANSPORT
        print(f"reap: reclaimed issue={num} ({reason})")
        reclaimed += 1

    print(f"reap: done in-progress={len(numbers)} reclaimed={reclaimed}")
    return EXIT_OK


def is_worker_comment(body):
    """Whether a comment was posted by a worker, by PROTOCOL.md §4's contract:
    every worker comment MUST *open* with the attribution disclaimer blockquote,
    so a comment whose FIRST line does not is (by the protocol's own definition)
    a human's. The match is derived from the DISCLAIMER constant — the prefix up
    to the worker-name backtick, so it is name-agnostic and never a second
    hand-kept copy of the format. Only the first line counts: an operator who
    quotes the disclaimer mid-reply is still a human."""
    prefix = DISCLAIMER.split("{worker}")[0]  # "> 🐙 **Kraken worker `"
    first_line = body.split("\n", 1)[0].rstrip("\r")
    return first_line.startswith(prefix)


def has_requeue_directive(body):
    """Whether a comment carries an EXPLICIT, STRUCTURED requeue directive — the
    only thing that bounces a DELIVERED (awaiting-merge) task back for rework, so
    a prose sentence merely starting a line with "requeue:" no longer bounces a
    ready branch by accident. Two accepted forms: a protocol/3
    `<!-- kraken {"type":"requeue"} -->` marker, or a standalone directive line
    whose only content is `requeue`/`requeue:` (case-insensitive)."""
    lines = body.split("\n")
    for raw in lines:
        marker = parse_marker(raw)
        if marker and marker.get("type") == "requeue":
            return True
    for raw in lines:
        if re.match(r"^\s*requeue:?\s*$", raw, re.IGNORECASE):
            return True
    return False


def cmd_requeue_check(args):
    """Requeue a held task when a genuine OPERATOR comment arrives
    (requeue-on-reply.yml). The triggering comment's body and author type come
    through the environment (COMMENT_BODY / COMMENT_AUTHOR_TYPE), never argv —
    the same untrusted-input discipline the workflow kept, so a comment carrying
    $(...) or backticks is only ever data. No-ops (never requeue): bot/self
    comments, worker comments (disclaimer present), and comments on an issue
    carrying no held label. needs-decision requeues on ANY bare operator comment;
    awaiting-merge (delivered) only on an explicit requeue directive. Exit 0
    always on a clean run, 20 on gh/transport failure."""
    repo, issue = args.repo, args.issue
    body = os.environ.get("COMMENT_BODY", "")
    author_type = os.environ.get("COMMENT_AUTHOR_TYPE", "")

    # Self/bot comments (the reaper's stale-claim:, this workflow's own
    # confirmation, the validator) never requeue — they carry no disclaimer but
    # are not human.
    if author_type == "Bot":
        print(f"requeue: bot/self comment on #{issue} — no-op")
        return EXIT_OK

    if is_worker_comment(body):
        print(f"requeue: worker comment (disclaimer present) on #{issue} — no-op")
        return EXIT_OK

    labels = issue_label_names(repo, issue)
    if labels is None:
        print(f"requeue: gh-failure stage=labels issue={issue}", file=sys.stderr)
        return EXIT_TRANSPORT

    if "needs-decision" in labels:
        if not swap_labels(repo, issue, remove="needs-decision"):
            print(f"requeue: gh-failure stage=label issue={issue}", file=sys.stderr)
            return EXIT_TRANSPORT
        if not post_comment(repo, issue,
                            "requeue: operator reply detected — needs-decision "
                            "removed, the task rejoins the queue with its full "
                            "thread as context."):
            print(f"requeue: gh-failure stage=comment issue={issue}", file=sys.stderr)
            return EXIT_TRANSPORT
        print(f"requeue: needs-decision removed on #{issue}")
        return EXIT_OK

    if "awaiting-merge" in labels:
        if has_requeue_directive(body):
            if not swap_labels(repo, issue, remove="awaiting-merge"):
                print(f"requeue: gh-failure stage=label issue={issue}", file=sys.stderr)
                return EXIT_TRANSPORT
            if not post_comment(repo, issue,
                                "requeue: explicit requeue on a delivered task — "
                                "awaiting-merge removed, the worker continues on "
                                "the existing branch."):
                print(f"requeue: gh-failure stage=comment issue={issue}", file=sys.stderr)
                return EXIT_TRANSPORT
            print(f"requeue: awaiting-merge removed on #{issue} (explicit requeue directive)")
            return EXIT_OK
        print(f"requeue: awaiting-merge on #{issue} left held (no explicit requeue directive) — no-op")
        return EXIT_OK

    print(f"requeue: #{issue} carries no held label — no-op")
    return EXIT_OK


# The issue-form headings the bundled task-template produces, and the field
# GitHub renders for a blank issue-form field. Section detection keys on these.
VALIDATION_MARKER = {"type": "validation"}
NO_RESPONSE_PLACEHOLDER = "_No response_"

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


def section_body(body, heading):
    """The trimmed content under `### HEADING` up to the next `### ` heading (or
    EOF). A hand-written issue lacking the heading yields nothing; an issue-form
    field left blank renders as the literal `_No response_`. Mirrors the awk the
    validate-task workflow used to carry."""
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


def is_empty_section(content):
    """True when a section's content is blank or only the issue-form
    `_No response_` placeholder — each line trimmed, blank lines dropped."""
    nonblank = [ln.strip() for ln in content.split("\n") if ln.strip() != ""]
    joined = "\n".join(nonblank)
    return joined == "" or joined == NO_RESPONSE_PLACEHOLDER


def validation_body(missing):
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


def latest_validation_comment(records):
    """The body of the newest prior validation comment (carrying the validation
    marker) in the thread, or None when none exists — the debounce anchor."""
    latest = None
    for rec in records:  # server order: keep the newest match
        body = rec.get("body") or ""
        if any((parse_marker(l) or {}).get("type") == "validation"
               for l in body.split("\n")):
            latest = body
    return latest


def cmd_validate(args):
    """Flag a queue entry missing its project label, Goal, or Acceptance
    (validate-task.yml). Reads the issue's live labels and body, and on any
    missing requirement posts ONE actionable comment naming exactly what to fix;
    a compliant task gets none (no noise on the happy path, and the same exit
    once the operator fixes what was flagged). Debounced: a re-run whose missing
    set is unchanged posts no duplicate. Informs only — never holds, closes, or
    relabels. Exit 0 on a clean run, 20 on gh/transport failure."""
    repo, issue = args.repo, args.issue

    labels = issue_label_names(repo, issue)
    if labels is None:
        print(f"validate: gh-failure stage=labels issue={issue}", file=sys.stderr)
        return EXIT_TRANSPORT
    if "kraken-task" not in labels:
        print(f"validate: #{issue} is not a kraken-task issue — no-op")
        return EXIT_OK

    body = issue_body(repo, issue)
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

    records = comment_records(repo, issue)
    if records is None:
        print(f"validate: gh-failure stage=comments issue={issue}", file=sys.stderr)
        return EXIT_TRANSPORT
    prior = latest_validation_comment(records)
    # rstrip: a re-read body may pick up a trailing newline the transport adds;
    # our own posted body never carries one, so normalizing both is exact.
    if prior is not None and prior.rstrip("\n") == body_to_post.rstrip("\n"):
        print(f"validate: #{issue} already carries an identical validation comment — no-op")
        return EXIT_OK

    if not post_comment(repo, issue, body_to_post):
        print(f"validate: gh-failure stage=comment issue={issue}", file=sys.stderr)
        return EXIT_TRANSPORT
    print(f"validate: #{issue} flagged (missing: project/Goal/Acceptance as listed)")
    return EXIT_OK


def is_identity_label(name):
    """A label cleanup MUST preserve on a closed task: the task-type label
    (kraken-task) and its project routing label (project:<name>). Everything else
    — every state-machine label (in-progress / needs-decision / awaiting-merge)
    and any unrelated label — is stripped, so a closed issue reads clean and
    label-based queue filters never match dead state (PROTOCOL.md §10)."""
    return name == "kraken-task" or name.startswith("project:")


def cmd_cleanup(args):
    """Strip every non-identity label off a CLOSED kraken-task issue except
    kraken-task itself and its project:<name> label (cleanup-closed.yml). Closing
    a task (the PR's `Closes` line, or a manual close) otherwise leaves whatever
    state-machine label it carried — awaiting-merge, needs-decision, even a stale
    in-progress — attached forever, so label-based filters keep matching dead
    state. A no-op when nothing but identity labels remain. The close event gates
    the workflow; this reads the issue's live labels and removes the rest, one at
    a time (idempotent — each removal targets a label the read just returned).
    Exit 0 on success, 20 on any gh/transport failure."""
    repo, issue = args.repo, args.issue

    labels = issue_label_names(repo, issue)
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
        if not swap_labels(repo, issue, remove=name):
            print(f"cleanup: gh-failure stage=remove issue={issue} label={name}",
                  file=sys.stderr)
            return EXIT_TRANSPORT
        stripped += 1

    print(f"cleanup: #{issue} done stripped={stripped}")
    return EXIT_OK


# is the read side of single-sourcing: the disclaimer format and the machine
# marker / claim-window vocabulary live in the constants above, and every other
# consumer derives from or is verified against them by executing this command
# rather than re-declaring the literals.
CONTRACT_FIELDS = {
    "disclaimer": lambda args: [disclaimer(args.worker)],
    "task-trailer": lambda args: [task_trailer(args.repo, args.issue, args.worker)],
    "reset-types": lambda args: list(RESET_TYPES),
    "liveness-types": lambda args: list(LIVENESS_TYPES),
    "marker-types": lambda args: list(MARKER_TYPES),
}


def cmd_contract(args):
    """Print an authoritative contract literal (no network). The single source of
    truth for the disclaimer format and the marker vocabulary — the requeue
    workflow filter, the test helpers, and the skill lint read these instead of
    re-declaring them, so a format change lands in exactly one place."""
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
        "reap",
        help="reclaim-stale.yml: move silent in-progress issues to needs-decision",
    )
    p.add_argument("repo")
    p.add_argument("--max-hours", type=int, default=None,
                   help="staleness threshold in hours (default: MAX_HOURS env, else 6)")
    p.set_defaults(func=cmd_reap)

    p = sub.add_parser(
        "requeue-check",
        help="requeue-on-reply.yml: requeue a held task on a genuine operator "
             "reply (reads COMMENT_BODY / COMMENT_AUTHOR_TYPE from the env)",
    )
    p.add_argument("repo")
    p.add_argument("issue")
    p.set_defaults(func=cmd_requeue_check)

    p = sub.add_parser(
        "validate",
        help="validate-task.yml: flag a task missing its project label, Goal, "
             "or Acceptance (debounced; informs only)",
    )
    p.add_argument("repo")
    p.add_argument("issue")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser(
        "cleanup",
        help="cleanup-closed.yml: strip every state/non-identity label off a "
             "closed task, keeping only kraken-task and project:<name>",
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
        help="print an authoritative contract literal (disclaimer / machine-line "
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


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
