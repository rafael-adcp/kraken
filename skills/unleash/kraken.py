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
that whole class of bug and lets pagination (the queue listing, the comment
history the coordination workflows read) be handled once, correctly.

Claiming (kraken-protocol/4) is a true compare-and-swap: creating the claim
ref `refs/kraken/claims/<issue>` succeeds for exactly one caller — GitHub
answers HTTP 422 to everyone else — so the ref IS the arbiter, and its commit's
server-stamped date IS the liveness clock. The retired protocol/3 comment
arbitration (the claim window, its reset markers, first-claim-wins over a
paginated re-read) existed only to compensate for writes that cannot fail on
conflict; with the CAS it is gone, and labels/comments are projection and
narrative, never the lock (see the claim-ref section below).

Transport (phase 1): `gh` stays the transport. Every GitHub call shells out to
`gh api` / `gh issue`, exactly like the scripts did, so the conformance stub
(which intercepts `gh` on PATH) and the operator's existing auth keep working
for free. A direct-REST phase 2 is possible later but out of scope here.
list-startable's queue fetch is the one exception to "shell out per call": it
batches labels, native blocked-by, and body into a single paginated
`gh api graphql` walk (classify_queue/fetch_open_tasks below), plus one
matching-refs read for the claim refs, so an idle watch poll costs O(pages),
not one REST call per non-held task.

Exit-code contract (PROTOCOL.md §12), preserved verbatim from the scripts:
    0   success
    10  lost the claim CAS — another worker holds the claim ref; back off
        (writing nothing) and pick the next candidate
    11  no longer clear — a held label appeared since listing; skip the task
    12  claim-next only: protocol-version handshake failed — this worker's
        PROTOCOL_VERSION disagrees with the coordination repo's vendored
        .github/kraken.py, or that file can't be read (fail closed). The drain
        refused before claiming; run `init --upgrade` (or upgrade the plugin)
    20  gh / network transport failure — state unknown, re-check before retry
    3   claim-next only: no candidate was startable — the queue is empty, or
        every candidate turned out held/lost as it iterated (nothing to claim,
        an honest empty result, not a fault)
    2   bad invocation (missing file / unknown mode)
"""

import argparse
import base64
import datetime
import hashlib
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
EXIT_PROTOCOL_MISMATCH = 12  # drain refused: local vs vendored PROTOCOL_VERSION disagree
EXIT_TRANSPORT = 20
EXIT_NONE = 3  # claim-next: nothing startable to claim (not empty-vs-error ambiguous)
EXIT_USAGE = 2

# A task carrying any of these is held, never startable. `in-progress` is the
# projection of the claim ref; the other two are operator-facing states.
HELD_LABELS = ("in-progress", "needs-decision", "awaiting-merge")

# The wire contract this program speaks (PROTOCOL.md): protocol/4 arbitrates the
# claim on a git-ref CAS and anchors liveness to the claim ref's commit date.
PROTOCOL_VERSION = 4

# Installed plugin version, single-sourced from the manifest the release workflow
# bumps; read at runtime for the Kraken-Task trailer, never a second literal.
PLUGIN_MANIFEST = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "..", ".claude-plugin", "plugin.json",
)
PLUGIN_VERSION_UNKNOWN = "unknown"

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))


def plugin_version(manifest=PLUGIN_MANIFEST):
    """Plugin version from the bundled `.claude-plugin/plugin.json`, or
    ``"unknown"`` if it is missing or unreadable."""
    try:
        with open(manifest, encoding="utf-8") as f:
            version = json.load(f).get("version")
    except (OSError, ValueError):
        return PLUGIN_VERSION_UNKNOWN
    return version if isinstance(version, str) and version else PLUGIN_VERSION_UNKNOWN

# --- structured hidden markers -----------------------------------------------
# A state-changing comment (or a claim ref's commit message) carries its machine
# payload in ONE hidden HTML-comment marker, e.g.
#     <!-- kraken {"type":"claim","worker":"env-1"} -->
# Compact ASCII-only JSON: json.dumps avoids the CRLF/quoting/prefix hazards of
# the old visible-line grammar, and ASCII keeps the reaper/requeue greps
# locale-independent. Under protocol/4 markers are audit trail only — the CAS on
# the claim ref arbitrates, never a marker.
MARKER_PREFIX = "<!-- kraken "
MARKER_SUFFIX = " -->"
MARKER_RE = re.compile(r"<!--\s*kraken\s+(\{.*?\})\s*-->")

# Every marker "type" this program emits — the protocol/4 vocabulary. `claim`
# and `heartbeat` ride the claim ref's commit message; the rest head comments.
# (`requeue` is operator-only.) The lint checks each against PROTOCOL.md's marker
# table via `kraken.py contract marker-types`.
MARKER_TYPES = ("claim", "heartbeat", "needs-decision", "delivered",
                "released", "stale-claim")


def make_marker(payload):
    """Render a machine payload dict as the hidden marker (compact, ASCII-only)."""
    return MARKER_PREFIX + json.dumps(payload, separators=(",", ":")) + MARKER_SUFFIX


def parse_marker(line):
    """Decode the kraken marker payload on a line, or None if it carries none.
    A body that is not a dict with a string "type" is treated as absent."""
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


# The attribution disclaimer — the ONE authoritative definition of its format.
# Every worker authenticates as the operator, so without this blockquote a worker
# comment reads like a human's. It heads every transition comment (a blank line
# must follow, or GitHub folds the body into the quote). Deliberately
# agent-agnostic ("a kraken tentacle", not "a Claude Code tentacle"); every other
# consumer derives from this constant via `kraken.py contract`.
DISCLAIMER = "> 🐙 **Kraken worker `{worker}`** — automated comment from a kraken tentacle, not a human."


def disclaimer(worker):
    return DISCLAIMER.format(worker=worker)


# The Kraken-Task commit trailer — the ONE authoritative definition of its format,
# the delivery-side twin of DISCLAIMER: it maps a merged commit back to the task
# and the plugin version. `{version}` comes from plugin_version(), never a pasted
# literal. The companion `Co-Authored-By` line stays the agent's own.
TASK_TRAILER = "Kraken-Task: {repo}#{issue} (worker: {worker}, kraken@{version})"


def task_trailer(repo, issue, worker):
    """Compose the authoritative `Kraken-Task:` commit trailer, stamping the live
    plugin version so `kraken@<version>` is never guessed."""
    return TASK_TRAILER.format(
        repo=repo, issue=issue, worker=worker, version=plugin_version()
    )



def compose_comment(worker, prose, payload):
    """Assemble a state-changing comment: disclaimer, human-facing prose, then the
    one hidden marker, blank-line separated so GitHub keeps them distinct."""
    parts = [disclaimer(worker)]
    prose = (prose or "").strip("\n")
    if prose:
        parts.append(prose)
    parts.append(make_marker(payload))
    return "\n\n".join(parts)


# --- transport ---------------------------------------------------------------

def run_gh_io(args, input_text=None):
    """Run `gh <args>`; return (returncode, stdout, stderr). Never raises on
    non-zero — the callers map a non-zero return to the exit-20 transport-failure
    path themselves, exactly where the scripts did `|| exit 20`. `input_text`
    feeds stdin (the `--input -` JSON body of the git-data writes); stderr is
    captured because it is where `gh api` names the HTTP status — the one signal
    that separates a lost claim CAS (HTTP 422) from a transport fault."""
    try:
        proc = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            input=input_text,
        )
    except FileNotFoundError:
        return 127, "", ""
    return proc.returncode, proc.stdout, proc.stderr


def run_gh(args):
    """`run_gh_io` for the many callers that need only (returncode, stdout)."""
    rc, out, _err = run_gh_io(args)
    return rc, out


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


def comment_records(repo, issue):
    """Every comment as a {"body", "createdAt"} record, in server order —
    paginated past 100 via the REST comments endpoint (`gh issue view --json
    comments` silently caps at 100: it does not page the nested GraphQL
    connection, so a long thread would read truncated). `status` and the
    validator's debounce read through here so no consumer ever sees a
    truncated history.

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
        # Skip inter-object whitespace: a pretty-printing jq spreads each object
        # across lines, so decode object-by-object, not line-by-line.
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


# --- claim refs: the protocol/4 CAS ------------------------------------------
#
# The claim is a git ref, refs/kraken/claims/<issue>. Creating a ref is the one
# common GitHub write that FAILS on conflict (422 to all but one creator), so the
# ref is the arbiter and the loser writes nothing. It points at an orphan commit
# whose message is the kraken marker and whose server-stamped date is the reaper's
# liveness clock. Refs are UI-invisible: the in-progress label is a projection
# written after the CAS.

CLAIM_REF_PREFIX = "refs/kraken/claims/"
# git's well-known empty-tree object, present in every repo, so an orphan commit
# needs no prior read; create_claim_commit falls back to HEAD's tree if a host
# rejects it.
EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def claim_ref(issue):
    return CLAIM_REF_PREFIX + str(issue)


def _is_http_422(err):
    """Whether a failed `gh api` call was an HTTP 422 — the CAS-lost signal on
    ref creation ("Reference already exists"), and the already-gone signal on
    ref deletion. gh prints the status on stderr as `... (HTTP 422)`."""
    return "(HTTP 422)" in (err or "")


def _head_tree_sha(repo):
    """The default branch's tree SHA — the fallback tree for hosts that reject
    the well-known empty-tree object. None on transport failure."""
    obj = gh_json(["api", f"repos/{repo}/commits/HEAD"])
    if obj is None:
        return None
    tree = (obj.get("commit") or {}).get("tree") or {}
    sha = tree.get("sha")
    return sha if isinstance(sha, str) and sha else None


def create_claim_commit(repo, payload):
    """Create the orphan commit a claim ref points at: empty tree, no parents,
    message = the kraken marker for `payload`. The server stamps the date, so the
    liveness clock is server-side. Returns the SHA, or None on transport failure."""
    for tree in (EMPTY_TREE_SHA, None):
        if tree is None:
            tree = _head_tree_sha(repo)
            if tree is None:
                return None
        body = json.dumps(
            {"message": make_marker(payload), "tree": tree, "parents": []}
        )
        rc, out, err = run_gh_io(
            ["api", f"repos/{repo}/git/commits", "--method", "POST",
             "--input", "-"],
            input_text=body,
        )
        if rc == 0:
            try:
                sha = json.loads(out).get("sha")
            except (ValueError, json.JSONDecodeError):
                return None
            return sha if isinstance(sha, str) and sha else None
        if not _is_http_422(err):
            return None
        # 422 on the empty tree: this host wants a reachable tree — fall back.
    return None


def claim_ref_create(repo, issue, sha):
    """The CAS itself. Returns "won" (ref created — this worker owns the task),
    "lost" (HTTP 422: another worker's ref already exists), or "fail"
    (transport — state unknown)."""
    body = json.dumps({"ref": claim_ref(issue), "sha": sha})
    rc, _out, err = run_gh_io(
        ["api", f"repos/{repo}/git/refs", "--method", "POST", "--input", "-"],
        input_text=body,
    )
    if rc == 0:
        return "won"
    if _is_http_422(err):
        return "lost"
    return "fail"


def claim_ref_update(repo, issue, sha):
    """Force-move the claim ref to a fresh commit — the heartbeat. True on
    success."""
    body = json.dumps({"sha": sha, "force": True})
    rc, _out, _err = run_gh_io(
        ["api", f"repos/{repo}/git/{claim_ref(issue)}", "--method", "PATCH",
         "--input", "-"],
        input_text=body,
    )
    return rc == 0


def claim_ref_delete(repo, issue):
    """Delete the claim ref — the lock release on every terminal transition.
    An already-missing ref (HTTP 422) counts as success: the lock is gone
    either way, and the delete stays idempotent under retries."""
    rc, _out, err = run_gh_io(
        ["api", f"repos/{repo}/git/{claim_ref(issue)}", "--method", "DELETE"],
    )
    return rc == 0 or _is_http_422(err)


def claim_ref_list(repo):
    """Every live claim ref as {issue_number: commit_sha}, in one paginated
    matching-refs read. Returns a dict (empty when none), or None on transport
    failure."""
    rc, out = run_gh([
        "api", f"repos/{repo}/git/matching-refs/kraken/claims/", "--paginate",
        "--jq", r'.[] | "\(.ref)\t\(.object.sha)"',
    ])
    if rc != 0:
        return None
    refs = {}
    for line in out.split("\n"):
        line = line.strip()
        if not line or "\t" not in line:
            continue
        ref, sha = line.split("\t", 1)
        tail = ref.rsplit("/", 1)[-1]
        try:
            refs[int(tail)] = sha
        except ValueError:
            continue
    return refs


def resolve_commit_meta(repo, shas):
    """Resolve each claim commit's {committedDate, message} in one batched
    GraphQL call (one aliased `object(oid:)` field per distinct SHA), never one
    call per ref — the resolve_depends_on pattern. Returns
    {sha: {"committedDate": ..., "message": ...}}, or None on transport
    failure."""
    if not shas:
        return {}
    owner, name = repo.split("/", 1)
    ordered = sorted(set(shas))
    fields = " ".join(
        f'c{i}: object(oid: "{sha}") {{ ... on Commit {{ committedDate message }} }}'
        for i, sha in enumerate(ordered)
    )
    resp = graphql(f'{{ repository(owner: "{owner}", name: "{name}") {{ {fields} }} }}')
    if resp is None:
        return None
    repo_obj = resp["data"]["repository"]
    meta = {}
    for i, sha in enumerate(ordered):
        obj = repo_obj.get(f"c{i}") or {}
        meta[sha] = {
            "committedDate": obj.get("committedDate") or "",
            "message": obj.get("message") or "",
        }
    return meta


def claim_ref_owner(repo, issue):
    """The worker named in the claim commit the ref for `issue` currently points
    at, or None when the ref is absent or unreadable. This is how a lost CAS
    (HTTP 422) is told apart: a 422 is a genuine loss only when the ref belongs
    to a DIFFERENT worker; a worker re-claiming its OWN in-flight claim after a
    network failure already owns the task (PROTOCOL.md §5's re-check caveat).
    None (transport/absent) is treated by the caller as 'not mine', so an
    ambiguous read never turns a real loss into a false win."""
    refs = claim_ref_list(repo)
    if refs is None:
        return None
    sha = refs.get(int(issue)) if str(issue).lstrip("-").isdigit() else None
    if not sha:
        return None
    meta = resolve_commit_meta(repo, [sha])
    if meta is None:
        return None
    payload = parse_marker((meta.get(sha) or {}).get("message") or "") or {}
    return payload.get("worker") or None


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
# Queue fetch and blocked-by check are batched through GraphQL, so an idle poll
# costs a queue-size-independent number of round trips. GraphQL's
# `issues(labels: [...])` is a UNION (unlike REST's AND), so we filter server-side
# on the single "kraken-task" label and match the project label client-side.

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
    from the win without a second fetch (the GraphQL walk already has it).

    Held means: a held label (the projection) OR a live claim ref (the lock).
    Reading both keeps the filter honest across the crash window between a won
    CAS and its label projection — a ref-held task is never offered as
    startable just because the label has not landed yet."""
    nodes = fetch_open_tasks(repo)
    if nodes is None:
        return None
    claim_refs = claim_ref_list(repo)
    if claim_refs is None:
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
        if any(h in label_names for h in HELD_LABELS) or number in claim_refs:
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
    """The one contended claim sequence — guard, CAS, projection — executed
    identically every time (PROTOCOL.md §5). Returns an exit code and prints a
    `claim:` diagnostic line. Shared by `claim` and `claim-next` so they can never
    drift."""

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

    # 2. CAS — orphan claim commit, then create the claim ref (only one creator wins).
    sha = create_claim_commit(repo, {"type": "claim", "worker": worker})
    if sha is None:
        print(f"claim: gh-failure issue={issue} stage=commit")
        return EXIT_TRANSPORT
    outcome = claim_ref_create(repo, issue, sha)
    if outcome == "fail":
        print(f"claim: gh-failure issue={issue} stage=ref")
        return EXIT_TRANSPORT
    if outcome == "lost":
        # A 422 is a real loss only if the ref is ANOTHER worker's. A worker
        # re-claiming its own in-flight ref after a network failure (§5) already
        # owns the task, so fall through to re-project. Unreadable owner = not ours.
        if claim_ref_owner(repo, issue) != worker:
            print(f"claim: lost-cas issue={issue} — another worker holds the claim ref")
            return EXIT_LOST

    # 3. Projection. State file FIRST so lifecycle hooks can release the claim even
    #    if the writes below fail; then the in-progress label and claim comment. A
    #    failure here leaves the claim HELD — exit 20 says re-check, reaper heals.
    write_claim_state(repo, issue, worker)
    if not swap_labels(repo, issue, add="in-progress"):
        print(f"claim: gh-failure issue={issue} stage=label (claim held)")
        return EXIT_TRANSPORT
    body = compose_comment(
        worker, "Claimed this task — starting work now.",
        {"type": "claim", "worker": worker},
    )
    if not post_comment(repo, issue, body):
        print(f"claim: gh-failure issue={issue} stage=comment (claim held)")
        return EXIT_TRANSPORT

    print(f"claim: claimed issue={issue} worker={worker}")
    return EXIT_OK


def cmd_claim(args):
    refused = refuse_second_claim(args.worker, args.issue)
    if refused is not None:
        return refused
    return _claim_once(args.repo, args.issue, args.worker)


# --- subcommand: claim-next --------------------------------------------------

def cmd_claim_next(args):
    """Collapse the deterministic claim loop into one invocation: list startable
    candidates oldest-first, then guard + CAS each in turn, stopping at the first
    win. Losses (10/11) move to the next candidate; a transport fault (20) stops
    with state-unknown; an exhausted queue is EXIT_NONE. Never retries a lost CAS
    on the same issue (PROTOCOL.md §5) — it iterates forward, never back."""
    repo, project, worker = args.repo, args.project, args.worker

    refused = refuse_second_claim(worker)
    if refused is not None:
        return refused

    # Protocol handshake (PROTOCOL.md drift guard): before the first claim, refuse
    # to drain if this worker's PROTOCOL_VERSION disagrees with the coordination
    # repo's vendored .github/kraken.py, or that file cannot be read (fail closed).
    ok, message = verify_protocol(repo)
    if not ok:
        print(message)
        return EXIT_PROTOCOL_MISMATCH

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
    """Liveness: force-move the claim ref to a fresh commit whose server-stamped
    date restarts the reaper's clock and whose marker carries the progress text.
    No timeline comment — `status` surfaces the age and message from the ref."""
    repo, issue, worker, message = args.repo, args.issue, args.worker, args.message
    sha = create_claim_commit(
        repo, {"type": "heartbeat", "worker": worker, "msg": message}
    )
    if sha is None:
        print(f"heartbeat: gh-failure issue={issue} stage=commit")
        return EXIT_TRANSPORT
    if not claim_ref_update(repo, issue, sha):
        print(f"heartbeat: gh-failure issue={issue} stage=ref")
        return EXIT_TRANSPORT
    print(f"heartbeat: advanced issue={issue} worker={worker}")
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
    # Comment and labels first, the lock last: a half-executed escalation leaves
    # the task held, not free with no question on record. A leftover ref is an
    # orphan lock the reaper deletes.
    if not claim_ref_delete(repo, issue):
        print(f"escalate: gh-failure issue={issue} stage=ref")
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
    # Result and labels first, the lock last (escalate's ordering rule).
    if not claim_ref_delete(repo, issue):
        print(f"deliver: gh-failure issue={issue} stage=ref")
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
    # The ref IS the claim: deleting it is what frees the task (comment and label
    # are narrative). Last, so the task never looks free while half-released.
    if not claim_ref_delete(repo, issue):
        print(f"release: gh-failure issue={issue} stage=ref")
        return EXIT_TRANSPORT

    clear_claim_state(worker)
    print(f"release: released issue={issue} worker={worker}")
    return EXIT_OK


# --- subcommand: watch -------------------------------------------------------

def snapshot_state(repo, project):
    """The queue snapshot list-startable emits in --snapshot mode, via the same
    classify_queue. Returns the snapshot text, or None on transport failure."""
    rows = classify_queue(repo, project)
    if rows is None:
        return None
    return "\n".join(
        f"{n}:{state}" for n, _, _, state in sorted(rows, key=lambda r: r[0])
    )


def wake_retry_due(flag_mtime, last_emit, retry_seconds, now):
    """Whether the watcher owes a lost-wake retry: the StopFailure hook stamped
    the wake-retry flag AFTER this watcher's last emission (that wake's turn died
    on a usage limit) and the retry spacing has elapsed. A flag older than the
    last emission is stale; no flag means no failed turn on record."""
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
            # Emit gate: a startable task exists AND either the queue changed or
            # a lost-wake retry is due. No blind re-emission timer.
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
# The operator console, mechanized (PROTOCOL.md §12): a read-only view — review
# queue, decision queue, in-flight with heartbeat ages, the merged-PR-but-open
# orphan heuristic, and launch recon — computed deterministically so the skill is
# a thin renderer and the data is reusable (`--json`). No write of any kind.
# Reuses fetch_open_tasks (queue), the claim refs (in-flight worker/age/progress),
# and paginated comment reads only for awaiting-merge tasks (the PR link).

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


def claim_meta_of(sha, commit_meta):
    """Decode one claim ref's commit into (worker, msg, anchor_iso) — the
    marker payload plus the server-stamped committedDate. This is the ONE
    liveness read `status` and the reaper share: the ref's commit date is the
    staleness clock, so nothing on the issue timeline (an operator poking a
    dead worker's thread, a bot comment) can ever make a claim look alive.
    Unreadable pieces come back as None, never guessed."""
    commit = commit_meta.get(sha) or {}
    payload = parse_marker(commit.get("message") or "") or {}
    worker = payload.get("worker") or None
    msg = payload.get("msg") or None
    anchor = commit.get("committedDate") or None
    return worker, msg, anchor


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


def compute_status(repo, project, nodes, now, *, claim_refs, commit_meta,
                   comment_reader, pr_merged, project_lister):
    """Pure-ish status computation, transport injected so it is unit-testable:
    given the queue nodes (from fetch_open_tasks), the claim refs + their commit
    meta, and reader callbacks, build the review/decision/in-flight/projects
    report. Returns the report dict, or None on any injected-transport failure
    (propagated as exit 20)."""
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
        elif "in-progress" in labels or number in claim_refs:
            # In flight: the label projection OR the lock itself — a claim whose
            # label has not landed yet is still running work.
            worker, msg, anchor = None, None, None
            age = None
            sha = claim_refs.get(number)
            if sha:
                worker, msg, anchor = claim_meta_of(sha, commit_meta)
                if anchor:
                    anchor_epoch = parse_iso(anchor)
                    if anchor_epoch is not None:
                        age = max(0, int(now - anchor_epoch))
            in_flight.append({"number": number, "title": title, "worker": worker,
                              "heartbeat_anchor": anchor,
                              "heartbeat_age_seconds": age,
                              "heartbeat_msg": msg})

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
        msg = item.get("heartbeat_msg")
        note = f" — {msg}" if msg else ""
        lines.append(f"     #{item['number']}  {item['title']}  · worker {worker} "
                     f"· last heartbeat {age} ago{note}")
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
    claim_refs = claim_ref_list(repo)
    if claim_refs is None:
        print("status: gh-failure stage=refs", file=sys.stderr)
        return EXIT_TRANSPORT
    commit_meta = resolve_commit_meta(repo, list(claim_refs.values()))
    if commit_meta is None:
        print("status: gh-failure stage=commits", file=sys.stderr)
        return EXIT_TRANSPORT

    report = compute_status(
        repo, project, nodes, time.time(),
        claim_refs=claim_refs,
        commit_meta=commit_meta,
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
# The bootstrap `init`, single-sourced here so the skill and the program never
# disagree on the asset set or the label canon.

# --- asset manifest: the hashes of every *released* version of each bundled ---
# asset. It exists to tell drift apart: an installed asset whose bytes hash to a
# value in this manifest was shipped by some plugin release and never hand-edited
# (so `init --upgrade` may safely replace it), while one matching no release is a
# deliberate customization (never overwritten). The *current* bundled bytes are
# hashed live at runtime — they need no manifest entry — so an in-development
# asset reads as `unchanged`, not stale. The manifest therefore records only
# PRIOR releases; a release appends each changed asset's new hash here.
#
# It covers ALL SIX assets, not just kraken.py: reclaim-stale.yml and
# cleanup-closed.yml flipped `permissions: contents: read` -> `write` between
# protocol/3 and protocol/4, so a stale workflow is as dangerous as a stale
# parser. Seeded with the real v0.4.0 (protocol/3) hashes — the exact drift this
# repo hit — so the repair path works on day one.
ASSET_MANIFEST = {
    "task-template.yml": [
        "4700580692e8b2b40d120f2e1e280dfa6bbfaea1eaae27f29ee7b42c78b3f6aa",
    ],
    "kraken.py": [
        "8ac696c864a6d169febe95698ec6d3d2ebef8844d3f3ff5e18ba2a26332b10ae",
    ],
    "reclaim-stale.yml": [
        "ffd162aa1f2d6d62927b17dfafedb77a01bd55f5854ab73b5aa184db5e4eb1cc",
    ],
    "cleanup-closed.yml": [
        "b2516a16da6b6ba982d4384278633e701b0bf699c017372e251a6ffacec94256",
    ],
    "requeue-on-reply.yml": [
        "8707c0af3e0a496461b9fcbe10833bfbff49e23c70a4b9c30f04e0ba856edc53",
    ],
    "validate-task.yml": [
        "156d002c42cb59b2a2e611dc5df652bde04e9102ebf92552a45ffe5ed06a733a",
    ],
}


def asset_sha256(data):
    """The sha256 hex digest of an asset's raw bytes — the manifest's key."""
    return hashlib.sha256(data).hexdigest()


def classify_asset(current, bundled, released):
    """Classify an installed asset against the bundled copy and its released
    hashes (a pure decision, so it is unit-testable without any network):

      - ``"absent"``     — nothing installed yet (``current is None``)
      - ``"unchanged"``  — byte-identical to the bundled copy
      - ``"outdated"``   — matches a KNOWN prior release, so it was shipped and
                           never hand-edited: safe for --upgrade to replace
      - ``"customized"`` — matches no release: a deliberate edit, never clobbered
    """
    if current is None:
        return "absent"
    if current == bundled:
        return "unchanged"
    if asset_sha256(current) in released:
        return "outdated"
    return "customized"


# --- protocol-version handshake ----------------------------------------------
# A worker reads the coordination repo's vendored `.github/kraken.py`
# PROTOCOL_VERSION and compares it with its own before draining. This turns the
# silent asset drift the manifest repairs into a loud, actionable refusal.
PROTOCOL_VERSION_RE = re.compile(r"^PROTOCOL_VERSION\s*=\s*(\d+)", re.MULTILINE)
VENDORED_KRAKEN_PATH = ".github/kraken.py"


def parse_protocol_version(text):
    """The PROTOCOL_VERSION integer declared in a kraken.py source, or None when
    the text is missing/unparseable (a value that cannot be read at all)."""
    if text is None:
        return None
    m = PROTOCOL_VERSION_RE.search(text)
    return int(m.group(1)) if m else None


def remote_protocol_version(repo):
    """The PROTOCOL_VERSION the coordination repo's vendored `.github/kraken.py`
    declares, or None if it cannot be read or parsed. One cheap contents-API
    read; fail-closed by construction — an unreadable file yields None, which
    verify_protocol turns into a refusal rather than a guess."""
    raw = gh_get_content(repo, VENDORED_KRAKEN_PATH)
    if raw is None:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return parse_protocol_version(text)


def verify_protocol(repo):
    """Compare this worker's PROTOCOL_VERSION with the coordination repo's
    vendored one. Returns (ok, message): ok is True only when both are readable
    and equal. A mismatch EITHER way — an old worker on an upgraded repo is as
    broken as the reverse — or an unreadable/unparseable vendored file refuses,
    with a message pointing at `init --upgrade` (fail closed)."""
    remote = remote_protocol_version(repo)
    if remote is None:
        return (False,
                "protocol handshake: cannot verify the coordination repo's "
                "protocol version — %s is missing or unreadable. Refusing to "
                "drain (fail closed). Run `kraken.py init --upgrade %s` to "
                "reinstall the vendored assets." % (VENDORED_KRAKEN_PATH, repo))
    if remote != PROTOCOL_VERSION:
        return (False,
                "protocol handshake: version mismatch — this worker speaks "
                "protocol/%d but %s vendors protocol/%d. Refusing to drain. "
                "Run `kraken.py init --upgrade %s` (or upgrade this worker's "
                "plugin) so both sides agree." % (
                    PROTOCOL_VERSION, VENDORED_KRAKEN_PATH, remote, repo))
    return (True, "")


# Each bundled asset init commits: (bundled filename, destination path in the
# coordination repo, create commit message).
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
    is absent (404) OR unreadable — a thin wrapper over gh_get_content_meta for
    callers that only need the bytes (e.g. the protocol handshake)."""
    content, _sha = gh_get_content_meta(repo, path)
    return content


def gh_get_content_meta(repo, path):
    """(bytes, blob_sha) for `path` on the repo via the contents API, or
    (None, None) when it is absent (404) OR unreadable. The blob sha is what the
    contents API requires to *update* an existing file — a create never sends
    one. The caller cannot tell 404 from a transport fault here and, like the
    skill, treats 'no readable file' as absent."""
    rc, out = run_gh(["api", f"/repos/{repo}/contents/{path}"])
    if rc != 0:
        return (None, None)
    try:
        obj = json.loads(out)
    except (ValueError, json.JSONDecodeError):
        return (None, None)
    encoded = obj.get("content")
    if not isinstance(encoded, str):
        return (None, None)
    try:
        content = base64.b64decode(re.sub(r"\s+", "", encoded))
    except ValueError:
        return (None, None)
    sha = obj.get("sha")
    return (content, sha if isinstance(sha, str) else None)


def gh_put_content(repo, path, data, message, sha=None):
    """Write `path` on the repo with `data` via the contents API. With no `sha`
    this creates the file (callers reach here only when the file was reported
    absent); passing the current blob `sha` UPDATES the existing file — GitHub
    rejects an overwrite that omits it, so `init --upgrade` always supplies it."""
    b64 = base64.b64encode(data).decode("ascii")
    api_args = [
        "api", f"/repos/{repo}/contents/{path}", "-X", "PUT",
        "-f", f"message={message}", "-f", f"content={b64}",
    ]
    if sha is not None:
        api_args += ["-f", f"sha={sha}"]
    rc, _ = run_gh(api_args)
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
    """Stand up (or repair) a coordination repo: verify-or-create it private,
    install the bundled assets, and upsert the canonical labels. Plain init is
    create-only and never overwrites an existing asset — it merely REPORTS one as
    `unchanged`, `outdated` (matches a prior release), or `customized` (matches
    none). `--upgrade` additionally replaces every `outdated` asset with the
    bundled copy (a customized asset is still never touched). Idempotent; touches
    no issues. Exit 0 on success, 20 on any gh/transport failure."""
    repo, project, upgrade = args.repo, args.project, args.upgrade
    report = {
        "repo": repo,
        "repo_status": "exists",
        "upgrade": bool(upgrade),
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

    # 2. Install/repair the bundled assets. A create-only pass by default; with
    #    --upgrade, an asset whose bytes match a KNOWN release (never hand-edited)
    #    is replaced. A customized asset is only ever flagged, never clobbered.
    for name, dest, message in INIT_ASSETS:
        try:
            with open(os.path.join(SKILL_DIR, name), "rb") as fh:
                bundled = fh.read()
        except OSError:
            print(f"init: missing bundled asset {name}", file=sys.stderr)
            return EXIT_USAGE
        current, sha = gh_get_content_meta(repo, dest)
        released = ASSET_MANIFEST.get(name, [])
        kind = classify_asset(current, bundled, released)
        if kind == "absent":
            if not gh_put_content(repo, dest, bundled, message):
                print(f"init: gh-failure stage=asset path={dest}", file=sys.stderr)
                return EXIT_TRANSPORT
            status = "created"
        elif kind == "outdated" and upgrade:
            if not gh_put_content(repo, dest, bundled,
                                  f"chore: upgrade kraken asset {dest}", sha=sha):
                print(f"init: gh-failure stage=asset path={dest}", file=sys.stderr)
                return EXIT_TRANSPORT
            status = "upgraded"
        else:
            # unchanged / outdated (no --upgrade) / customized — write nothing.
            status = kind
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
    count = lambda s: sum(1 for a in report["assets"] if a["status"] == s)
    created, unchanged = count("created"), count("unchanged")
    outdated, upgraded, customized = count("outdated"), count("upgraded"), count("customized")
    lines.append(
        f"init: done repo={report['repo']} repo_status={report['repo_status']} "
        f"assets_created={created} assets_unchanged={unchanged} "
        f"assets_outdated={outdated} assets_upgraded={upgraded} "
        f"assets_customized={customized} labels={len(report['labels'])}"
    )
    # Actionable hints: outdated assets under a plain init can be repaired with
    # --upgrade; a customized asset is a deliberate edit left for the operator.
    if outdated and not report.get("upgrade"):
        lines.append(
            f"init: hint {outdated} asset(s) match an OLDER release — re-run "
            f"`init --upgrade {report['repo']}` to reinstall the bundled copies")
    if customized:
        lines.append(
            f"init: hint {customized} asset(s) differ from every known release "
            f"(customized) — left untouched; reconcile them by hand if intended")
    return "\n".join(lines)



# --- coordination-repo workflow subcommands ----------------------------------
# The three logic-bearing workflows (reclaim-stale, requeue-on-reply,
# validate-task) run their logic here rather than re-implementing the protocol
# parse in jq/grep/awk — ONE parser, sharing the marker decoder, disclaimer, and
# label vocabulary (and unit tests) with the worker side. Each workflow is a
# checkout + a single exec of the matching subcommand below.

# The reaper's default staleness threshold, in hours. A claim ref older than this
# — or unreadable — belongs to a dead worker and is reclaimed to needs-decision.
# The workflow passes it through the MAX_HOURS env var.
REAP_DEFAULT_MAX_HOURS = 6


def stale_claim_body(reason):
    """The reaper's reclaim comment: human prose plus the stale-claim marker
    (audit trail). It carries NO attribution disclaimer: the reaper is not a
    worker but the coordination repo's own automation, authored server-side by
    the Actions bot (user.type == Bot), which is exactly how requeue-on-reply's
    Bot gate tells it apart from an operator comment."""
    marker = make_marker({"type": "stale-claim", "reason": reason})
    prose = (
        f"The worker has gone silent ({reason}) and likely died. To requeue, "
        "remove the needs-decision label; or investigate first."
    )
    return f"{prose}\n\n{marker}"


def orphan_projection_body():
    """The reconciler's requeue note for an in-progress label with no claim ref
    behind it — a crashed release, or a claim made before protocol/4. Same
    Actions-bot authorship as stale_claim_body, same no-disclaimer rule."""
    marker = make_marker({
        "type": "stale-claim",
        "reason": "in-progress label with no claim ref",
    })
    prose = (
        "This task carried the in-progress label with no live claim ref behind "
        "it (a crashed release, or a claim from before protocol/4). The label "
        "was removed and the task rejoins the queue."
    )
    return f"{prose}\n\n{marker}"


def resolve_issue_meta(repo, numbers):
    """Each issue's open/closed state and label names in one batched GraphQL
    call (one aliased `iN: issue(number: N)` field per number) — the
    reconciler's per-ref read, never one call per issue. Returns
    {number: (is_open, [label names])}, or None on transport failure."""
    if not numbers:
        return {}
    owner, name = repo.split("/", 1)
    fields = " ".join(
        f"i{n}: issue(number: {n}) {{ state labels(first: 20) {{ nodes {{ name }} }} }}"
        for n in numbers
    )
    resp = graphql(f'{{ repository(owner: "{owner}", name: "{name}") {{ {fields} }} }}')
    if resp is None:
        return None
    repo_obj = resp["data"]["repository"]
    meta = {}
    for n in numbers:
        obj = repo_obj.get(f"i{n}") or {}
        is_open = str(obj.get("state", "")).upper() == "OPEN"
        labels = [l.get("name", "")
                  for l in (obj.get("labels") or {}).get("nodes", [])]
        meta[n] = (is_open, labels)
    return meta


def cmd_reap(args):
    """The reconciler (reclaim-stale.yml). The claim ref is the lock and its
    commit date the liveness clock, so reap reads every claim ref plus the
    in-progress projection and makes the two agree (PROTOCOL.md §6):

      1. a ref on a closed or needs-decision/awaiting-merge issue is an orphan
         lock (a terminal transition crashed before its ref delete) — delete
         the ref, touch nothing else;
      2. a ref older than MAX_HOURS — or whose commit cannot be read: nothing
         proves the worker alive — is a dead worker's claim: post the
         stale-claim comment, move the task to needs-decision, delete the ref
         (lock last, so a crashed reap leaves the task held and rule 1 finishes
         the job next pass);
      3. a fresh ref on an issue missing its in-progress label is a claim whose
         projection crashed — heal by adding the label;
      4. an OPEN in-progress issue with NO ref is an orphan projection (a
         crashed release, or a claim from before protocol/4) — remove the label
         so the task requeues, with a bot note saying why.

    Nothing on the issue timeline anchors liveness — an operator poking a dead
    worker's thread never resets the clock. Exit 0 on success, 20 on any
    gh/transport failure."""
    repo = args.repo
    max_hours = args.max_hours
    if max_hours is None:
        try:
            max_hours = int(os.environ.get("MAX_HOURS", REAP_DEFAULT_MAX_HOURS))
        except ValueError:
            max_hours = REAP_DEFAULT_MAX_HOURS
    now = time.time()

    refs = claim_ref_list(repo)
    if refs is None:
        print("reap: gh-failure stage=refs", file=sys.stderr)
        return EXIT_TRANSPORT
    commit_meta = resolve_commit_meta(repo, list(refs.values()))
    if commit_meta is None:
        print("reap: gh-failure stage=commits", file=sys.stderr)
        return EXIT_TRANSPORT
    issue_meta = resolve_issue_meta(repo, sorted(refs))
    if issue_meta is None:
        print("reap: gh-failure stage=issues", file=sys.stderr)
        return EXIT_TRANSPORT
    progress_nums = open_issue_numbers(repo, "in-progress")
    if progress_nums is None:
        print("reap: gh-failure stage=list", file=sys.stderr)
        return EXIT_TRANSPORT

    reclaimed = orphan_locks = healed = requeued = 0
    for num in sorted(refs):
        is_open, labels = issue_meta.get(num, (False, []))

        # 1. Orphan lock: the task already left the claim by a terminal
        #    transition (or closed); only the ref delete was lost.
        if not is_open or "needs-decision" in labels or "awaiting-merge" in labels:
            if not claim_ref_delete(repo, num):
                print(f"reap: gh-failure stage=ref issue={num}", file=sys.stderr)
                return EXIT_TRANSPORT
            print(f"reap: orphan-lock issue={num} — claim ref deleted")
            orphan_locks += 1
            continue

        anchor_epoch = parse_iso(
            (commit_meta.get(refs[num]) or {}).get("committedDate") or ""
        )
        if anchor_epoch is None:
            stale = True  # nothing proves the worker alive — infinitely stale
            reason = "no readable heartbeat on the claim ref"
        else:
            age_hours = int((now - anchor_epoch) // 3600)
            stale = age_hours >= max_hours
            reason = f"no worker heartbeat for {age_hours}h"

        if stale:
            # 2. Dead worker: reclaim for triage, then release the lock.
            if not swap_labels(
                repo, num,
                remove="in-progress" if "in-progress" in labels else None,
                add="needs-decision",
            ):
                print(f"reap: gh-failure stage=labels issue={num}", file=sys.stderr)
                return EXIT_TRANSPORT
            if not post_comment(repo, num, stale_claim_body(reason)):
                print(f"reap: gh-failure stage=comment issue={num}", file=sys.stderr)
                return EXIT_TRANSPORT
            if not claim_ref_delete(repo, num):
                print(f"reap: gh-failure stage=ref issue={num}", file=sys.stderr)
                return EXIT_TRANSPORT
            print(f"reap: reclaimed issue={num} ({reason})")
            reclaimed += 1
            continue

        # 3. Live claim: heal a label projection that never landed.
        if "in-progress" not in labels:
            if not swap_labels(repo, num, add="in-progress"):
                print(f"reap: gh-failure stage=heal issue={num}", file=sys.stderr)
                return EXIT_TRANSPORT
            print(f"reap: healed issue={num} — in-progress label restored")
            healed += 1

    # 4. Orphan projection: in-progress with no lock behind it — requeue.
    for num in progress_nums:
        if num in refs:
            continue
        if not swap_labels(repo, num, remove="in-progress"):
            print(f"reap: gh-failure stage=requeue issue={num}", file=sys.stderr)
            return EXIT_TRANSPORT
        if not post_comment(repo, num, orphan_projection_body()):
            print(f"reap: gh-failure stage=comment issue={num}", file=sys.stderr)
            return EXIT_TRANSPORT
        print(f"reap: requeued issue={num} (in-progress label with no claim ref)")
        requeued += 1

    print(
        f"reap: done refs={len(refs)} reclaimed={reclaimed} "
        f"orphan_locks={orphan_locks} healed={healed} requeued={requeued}"
    )
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

    # Self/bot comments (the reaper's stale-claim:, this workflow's confirmation,
    # the validator) never requeue — no disclaimer, but not human.
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


# The issue-form headings the bundled task-template produces, and the placeholder
# GitHub renders for a blank field. Section detection keys on these.
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

    # A closed task must not leave its lock behind: a crashed worker's claim
    # ref could linger past the close (claim_ref_delete tolerates one that is
    # already gone, so this stays idempotent).
    if not claim_ref_delete(repo, issue):
        print(f"cleanup: gh-failure stage=ref issue={issue}", file=sys.stderr)
        return EXIT_TRANSPORT

    print(f"cleanup: #{issue} done stripped={stripped}")
    return EXIT_OK


# --- subcommand: contract ----------------------------------------------------
# The read side of single-sourcing: consumers fetch the disclaimer format and
# marker vocabulary from here instead of re-declaring the literals.
CONTRACT_FIELDS = {
    "disclaimer": lambda args: [disclaimer(args.worker)],
    "task-trailer": lambda args: [task_trailer(args.repo, args.issue, args.worker)],
    "marker-types": lambda args: list(MARKER_TYPES),
}


def cmd_contract(args):
    """Print an authoritative contract literal (no network) — the single source of
    truth for the disclaimer format and marker vocabulary, so a format change lands
    in one place."""
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

    p = sub.add_parser("watch", help="poll the queue, print on a startable change")
    p.add_argument("repo")
    p.add_argument("project")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser(
        "reap",
        help="reclaim-stale.yml: reconcile claim refs with labels — reclaim "
             "stale claims, delete orphan locks, heal/requeue projections",
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
    p.add_argument("--upgrade", action="store_true",
                   help="replace installed assets that match an older release "
                        "with the bundled copy (customized assets stay untouched)")
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


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
