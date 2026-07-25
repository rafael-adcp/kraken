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

Claiming (kraken-protocol/5) is a true compare-and-swap: creating the claim
ref `refs/kraken/claims/<issue>` succeeds for exactly one caller — GitHub
answers HTTP 422 to everyone else — so the ref IS the arbiter, and its commit's
server-stamped date IS the liveness clock. The retired protocol/3 comment
arbitration (the claim window, its reset markers, first-claim-wins over a
paginated re-read) existed only to compensate for writes that cannot fail on
conflict; with the CAS it is gone, and labels/comments are projection and
narrative, never the lock (see the claim-ref section below).

Transport (phase 2): direct REST/GraphQL over stdlib `urllib.request` — no
per-call `gh` subprocess. Every GitHub call is an HTTP request whose status
code is a first-class integer, so the protocol's strongest guarantee (exactly
one CAS winner, told apart by HTTP 422) is decided by `status == 422`, never by
scraping an external binary's stderr. The API base comes from the
GITHUB_API_URL env var (the variable GitHub Actions already sets; default
https://api.github.com) — also the conformance suite's seam, which points it at
a local stateful stub server. The token comes from GH_TOKEN / GITHUB_TOKEN,
falling back to ONE memoized `gh auth token` spawn per process (startup cost,
never per call). Queue reads stay batched: labels, native blocked-by, and body
ride a single paginated GraphQL walk (classify_queue/fetch_open_tasks below),
plus one paginated matching-refs read for the claim refs, so an idle watch poll
costs O(pages), not one REST call per non-held task.

Exit-code contract (PROTOCOL.md §12), preserved verbatim from the scripts:
    0   success
    10  lost the claim CAS — another worker holds the claim ref; back off
        (writing nothing) and pick the next candidate
    11  no longer clear — a held label appeared since listing; skip the task
    12  claim-next only: drift handshake failed — the coordination repo's
        vendored .github/kraken.py differs from this worker's bundled copy, or
        that file can't be read (fail closed). The drain refused before
        claiming; run `init --upgrade` (or upgrade the plugin)
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
import urllib.error
import urllib.parse
import urllib.request

# Exit codes — the agent branches on these; keep them identical to the scripts.
EXIT_OK = 0
EXIT_LOST = 10
EXIT_NOT_CLEAR = 11
EXIT_PROTOCOL_MISMATCH = 12  # drain refused: vendored assets drifted from the bundled copy
EXIT_UNKNOWN_PROJECT = 13  # drain refused: the repo has no project:<name> label
EXIT_TRANSPORT = 20
EXIT_NONE = 3  # claim-next: nothing startable to claim (not empty-vs-error ambiguous)
EXIT_USAGE = 2

# A task carrying any of these is held, never startable. `in-progress` is the
# projection of the claim ref; the other two are operator-facing states.
HELD_LABELS = ("in-progress", "needs-decision", "awaiting-merge")

# A scheduling preference, not a state: a startable task carrying this label is
# offered ahead of normal ones (createdAt FIFO still breaks ties within each
# tier). Not a coordination invariant — an old worker that ignores it simply
# falls back to pure FIFO, so honoring it needs no PROTOCOL_VERSION bump.
PRIORITY_LABEL = "priority:high"

# The wire contract this program speaks (PROTOCOL.md): protocol/4 arbitrates the
# claim on a git-ref CAS and anchors liveness to the claim ref's commit date;
# protocol/5 moves the reconcile of that clock onto the READER (§6), so a
# conforming coordination repo runs no scheduled job of its own.
PROTOCOL_VERSION = 5

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
# `note` heads a free-form worker comment and changes no machine state — it
# exists only so the comment is recognizable as worker-authored (§4). (`requeue`
# is operator-only.) The lint checks each against PROTOCOL.md's marker table via
# `kraken.py contract marker-types`.
MARKER_TYPES = ("claim", "heartbeat", "needs-decision", "delivered",
                "released", "stale-claim", "note")


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


def compose_note(worker, prose):
    """A free-form worker comment (assumptions, progress prose): the attribution
    disclaimer, the prose, then a single non-state-changing `note` marker
    (PROTOCOL.md §4), blank-line separated so GitHub keeps them distinct. The
    marker is what makes the §6 requeue derivation read this as a worker comment
    *structurally* — the disclaimer stays as human-facing attribution but is no
    longer the arbiter. A `note` marker carries no machine state: the reconcile,
    the requeue derivation, and validate all treat it as inert (they key on their
    own marker types)."""
    return compose_comment(worker, prose, {"type": "note", "worker": worker})


# --- transport ---------------------------------------------------------------
# Direct HTTP over stdlib urllib. `http_request` is the ONE network touchpoint
# (and the unit tests' patch seam); everything else builds on it. A non-2xx
# answer is an integer status the caller branches on — HTTP 422 is the CAS-lost
# signal on the claim surface, everything else non-2xx is the exit-20
# transport-failure path.

DEFAULT_API_URL = "https://api.github.com"
HTTP_TIMEOUT_SECONDS = 30
# Sentinel for "no HTTP answer at all" (DNS, refused, timeout). Not a real
# status code, so every `status == N` decision reads it as a transport fault.
STATUS_NETWORK_FAILURE = 0
# GitHub caps per_page at 100; a page shorter than this is the last one.
PER_PAGE = 100

_TOKEN_CACHE = {"resolved": False, "token": ""}


def api_base():
    """The API root every request is made against. GITHUB_API_URL is both the
    GitHub Actions convention (set on every runner, GHES included) and the
    conformance seam (tests point it at a local stub server)."""
    return (os.environ.get("GITHUB_API_URL") or DEFAULT_API_URL).rstrip("/")


def github_token():
    """The API token: GH_TOKEN, then GITHUB_TOKEN, then ONE `gh auth token`
    spawn, memoized for the process lifetime — a startup cost, never a per-call
    one. Empty when nothing yields a token (requests then go out
    unauthenticated and surface as transport failures on a private repo)."""
    if not _TOKEN_CACHE["resolved"]:
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
        if not token:
            try:
                proc = subprocess.run(
                    ["gh", "auth", "token"],
                    capture_output=True, text=True, encoding="utf-8",
                )
                if proc.returncode == 0:
                    token = proc.stdout.strip()
            except OSError:
                token = ""
        _TOKEN_CACHE["token"] = token
        _TOKEN_CACHE["resolved"] = True
    return _TOKEN_CACHE["token"]


def http_request(method, path, body=None):
    """One API call: return (status, text). `path` starts with "/"; a dict
    `body` is sent as JSON. Never raises — a request that got no HTTP answer at
    all comes back as (STATUS_NETWORK_FAILURE, ""), and a non-2xx answer keeps
    its real integer status so the caller can tell a CAS-lost 422 from a fault."""
    url = api_base() + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    token = github_token()
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
            return resp.getcode(), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        try:
            text = exc.read().decode("utf-8", "replace")
        except OSError:
            text = ""
        return exc.code, text
    except (urllib.error.URLError, OSError, ValueError):
        return STATUS_NETWORK_FAILURE, ""


def http_json(method, path, body=None):
    """Parsed JSON from a 2xx response, or None on any failure (non-2xx,
    network fault, undecodable body) — the exit-20 mapping for plain reads."""
    status, text = http_request(method, path, body)
    if not 200 <= status < 300:
        return None
    try:
        return json.loads(text)
    except (ValueError, json.JSONDecodeError):
        return None


def http_paginated(path):
    """Every element of a paginated list endpoint, concatenated across
    per_page/page requests (a page shorter than PER_PAGE is the last).
    Returns a list, or None on transport failure."""
    sep = "&" if "?" in path else "?"
    items = []
    page = 1
    while True:
        chunk = http_json("GET", f"{path}{sep}per_page={PER_PAGE}&page={page}")
        if not isinstance(chunk, list):
            return None
        items.extend(chunk)
        if len(chunk) < PER_PAGE:
            return items
        page += 1


def quote_path(segment):
    """URL-quote one path segment (a label name, a contents path component)."""
    return urllib.parse.quote(segment, safe="")


def graphql(query):
    """POST a GraphQL query; return the parsed {"data": ...} envelope, or None
    on transport / decode / GraphQL-error failure."""
    resp = http_json("POST", "/graphql", {"query": query})
    if not isinstance(resp, dict) or resp.get("errors"):
        return None
    if not isinstance(resp.get("data"), dict):
        return None
    return resp


def comment_records(repo, issue):
    """Every comment as a {"body", "createdAt"} record, in server order —
    paginated past 100 via the REST comments endpoint (a single-page read
    would silently truncate a long thread). `status` and the validator's
    debounce read through here so no consumer ever sees a truncated history.
    REST spells the timestamp `created_at`; it is mapped to the createdAt
    shape every consumer reads. Returns a list, or None on transport failure."""
    items = http_paginated(f"/repos/{repo}/issues/{issue}/comments")
    if items is None:
        return None
    return [
        {"body": c.get("body") or "", "createdAt": c.get("created_at") or ""}
        for c in items if isinstance(c, dict)
    ]


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


def _head_tree_sha(repo):
    """The default branch's tree SHA — the fallback tree for hosts that reject
    the well-known empty-tree object. None on transport failure."""
    obj = http_json("GET", f"/repos/{repo}/commits/HEAD")
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
        status, text = http_request(
            "POST", f"/repos/{repo}/git/commits",
            {"message": make_marker(payload), "tree": tree, "parents": []},
        )
        if 200 <= status < 300:
            try:
                sha = json.loads(text).get("sha")
            except (ValueError, json.JSONDecodeError):
                return None
            return sha if isinstance(sha, str) and sha else None
        if status != 422:
            return None
        # 422 on the empty tree: this host wants a reachable tree — fall back.
    return None


def claim_ref_create(repo, issue, sha):
    """The CAS itself. Returns "won" (ref created — this worker owns the task),
    "lost" (HTTP 422: another worker's ref already exists), or "fail"
    (transport — state unknown). The verdict is the integer status code,
    nothing else."""
    status, _text = http_request(
        "POST", f"/repos/{repo}/git/refs",
        {"ref": claim_ref(issue), "sha": sha},
    )
    if 200 <= status < 300:
        return "won"
    if status == 422:
        return "lost"
    return "fail"


def claim_ref_update(repo, issue, sha):
    """Force-move the claim ref to a fresh commit — the heartbeat. True on
    success."""
    status, _text = http_request(
        "PATCH", f"/repos/{repo}/git/{claim_ref(issue)}",
        {"sha": sha, "force": True},
    )
    return 200 <= status < 300


def claim_ref_delete(repo, issue):
    """Delete the claim ref — the lock release on every terminal transition.
    An already-missing ref (HTTP 422) counts as success: the lock is gone
    either way, and the delete stays idempotent under retries."""
    status, _text = http_request(
        "DELETE", f"/repos/{repo}/git/{claim_ref(issue)}"
    )
    return 200 <= status < 300 or status == 422


def claim_ref_list(repo):
    """Every live claim ref as {issue_number: commit_sha}, in one paginated
    matching-refs read. Returns a dict (empty when none), or None on transport
    failure."""
    items = http_paginated(f"/repos/{repo}/git/matching-refs/kraken/claims/")
    if items is None:
        return None
    refs = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        ref = item.get("ref") or ""
        sha = (item.get("object") or {}).get("sha") or ""
        tail = ref.rsplit("/", 1)[-1]
        if not sha:
            continue
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
    if not str(issue).lstrip("-").isdigit():
        return None
    # GET the single ref by exact name (git/ref/, singular) — one unpaginated
    # call, not the git/matching-refs/ prefix scan of every live claim. A 404
    # is the ref simply being absent (like swap_labels' remove); any OTHER
    # non-2xx is a transport fault. Both yield None, so an ambiguous read never
    # fabricates an owner and never turns a real CAS loss into a false win.
    status, text = http_request(
        "GET", f"/repos/{repo}/git/ref/kraken/claims/{int(issue)}"
    )
    if not 200 <= status < 300:
        return None
    try:
        obj = json.loads(text)
    except (ValueError, json.JSONDecodeError):
        return None
    sha = ((obj.get("object") or {}).get("sha") or "") if isinstance(obj, dict) else ""
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
    status, _text = http_request(
        "POST", f"/repos/{repo}/issues/{issue}/comments", {"body": body}
    )
    return 200 <= status < 300


def swap_labels(repo, issue, remove=None, add=None):
    """Remove and/or add one label on an issue. A remove of a label the issue
    no longer carries (HTTP 404) counts as success, so the swap stays
    idempotent under retries and reaper races — the same tolerance `gh issue
    edit --remove-label` had."""
    if remove:
        status, _text = http_request(
            "DELETE",
            f"/repos/{repo}/issues/{issue}/labels/{quote_path(remove)}",
        )
        if not (200 <= status < 300 or status == 404):
            return False
    if add:
        status, _text = http_request(
            "POST", f"/repos/{repo}/issues/{issue}/labels", {"labels": [add]}
        )
        if not 200 <= status < 300:
            return False
    return True


def issue_detail(repo, issue):
    """The live issue object (labels, body, state, ...), or None on transport
    failure — one GET serves both the label and body readers."""
    return http_json("GET", f"/repos/{repo}/issues/{issue}")


def issue_label_names(repo, issue):
    """The label names currently on an issue, live. Returns a list, or None on
    transport failure — the coordination workflows read labels off the live
    issue (a labeled/edited event may have just changed them)."""
    obj = issue_detail(repo, issue)
    if obj is None:
        return None
    return [lbl.get("name", "") for lbl in obj.get("labels", [])]


def issue_body(repo, issue):
    """The issue's body text, live. Returns a string ("" when the body is empty
    or null), or None on transport failure."""
    obj = issue_detail(repo, issue)
    if obj is None:
        return None
    return obj.get("body") or ""


# --- subcommand: list-startable ---------------------------------------------
#
# Queue fetch and blocked-by check are batched through GraphQL, so an idle poll
# costs a queue-size-independent number of round trips. GraphQL's
# `issues(labels: [...])` is a UNION (unlike REST's AND), so we filter server-side
# on the single "kraken-task" label and match the project label client-side.

# How many trailing comments the queue walk carries per task. The requeue
# derivation (§6) only ever needs the NEWEST comment of two classes — the last
# worker comment and any operator reply after it — so a window cannot mislead it:
# a window holding no worker comment means the last one is older than the window,
# which makes every operator comment inside it newer, the correct verdict. 25 is
# therefore about payload, not correctness.
QUEUE_COMMENT_WINDOW = 25


def fetch_open_tasks(repo):
    """Every OPEN kraken-task issue in the repo, across all projects — number,
    title, createdAt, body, labels, native blocked-by, and the trailing comment
    window the requeue derivation reads, all in one paginated GraphQL walk.
    Returns the node list, or None on transport failure."""
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
            f'comments(last: {QUEUE_COMMENT_WINDOW}) {{ nodes {{ body createdAt }} }} '
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


# --- the requeue derivation (PROTOCOL.md §6) ---------------------------------
# When the operator answers a held task, the task rejoins the queue. Through
# protocol/4 that was a MUTATION: a workflow watched the comment stream and
# removed the holding label. But "an operator replied after the last worker
# comment" is a property of the thread, readable at any time — so protocol/5
# DERIVES it on the read instead. No workflow, no label write, and no window
# where the queue disagrees with the thread.
#
# The comment window rides the queue walk (QUEUE_COMMENT_WINDOW), so the
# derivation costs no request of its own.

# The held labels an operator reply can lift. `in-progress` is deliberately NOT
# one: an in-progress label with no ref behind it is the reconciler's rule 4
# (§6), a repair, not a requeue — two mechanisms for one label would race.
REQUEUEABLE_LABELS = ("needs-decision", "awaiting-merge")


def is_worker_comment(body):
    """Whether a comment was posted by a worker — the requeue derivation's
    discriminator (PROTOCOL.md §4). **Structural**: a worker comment is one that
    carries a hidden kraken marker on ANY line. Every worker-posted comment wears
    one (the transition markers for state changes, the `note` marker for a
    free-form note), so marker presence — not the presentation text — is the
    arbiter, closing the fragility class kraken-protocol removed everywhere else.
    It also subsumes the coordination repo's own bot comments (the validator's
    carry a `validation` marker), which is why the derivation needs no author
    metadata in the query at all.

    A comment whose FIRST line opens with the attribution disclaimer also counts,
    as a back-compat fallback for legacy threads and any comment written before
    `note` gained its marker (the prefix is derived from the DISCLAIMER constant,
    never a second copy). The disclaimer stays the human-facing attribution but is
    no longer load-bearing for requeue.

    Accepted edge (§4): an operator who pastes a raw kraken marker into a reply is
    read as a worker and will not requeue — the same class of edge as an operator
    quoting the disclaimer on the first line; removing the held label by hand is
    the escape hatch."""
    if any(parse_marker(line) is not None for line in body.split("\n")):
        return True
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


def comment_nodes_of(node):
    """The queue walk's trailing comment window for a task node, oldest-first."""
    return (node.get("comments") or {}).get("nodes") or []


def requeue_verdict(held, comments):
    """Whether an operator reply has requeued a task held by `held` labels — a
    PURE read of the thread (PROTOCOL.md §6), so it is decided identically by
    every reader and needs no write to become true.

    The rule: an operator comment (one carrying no kraken marker) newer than the
    newest worker comment. The two held states stay asymmetric — a bare reply
    requeues `needs-decision` (a human comment is almost always the answer), but
    `awaiting-merge` is already *delivered* and stays held unless a reply carries
    an explicit requeue directive, so a `requeue:` buried in prose cannot bounce a
    ready branch.

    Ordering is the comment list's own (GitHub returns a thread oldest-first);
    nothing here reads a timestamp, so a re-posted comment cannot reorder the
    verdict."""
    worker_at = -1
    for i, rec in enumerate(comments):
        if is_worker_comment(rec.get("body") or ""):
            worker_at = i
    replies = [rec for i, rec in enumerate(comments)
               if i > worker_at and not is_worker_comment(rec.get("body") or "")]
    if not replies:
        return False
    if "awaiting-merge" in held:
        return any(has_requeue_directive(rec.get("body") or "") for rec in replies)
    return True


def requeued_labels(node, claim_refs):
    """The held labels this task carries that an operator reply has already
    lifted — a tuple, empty when the task is genuinely held. Pure, so the
    classification and the claim that follows it reach the same verdict from the
    same node without a second read.

    A live claim ref outranks any reading of the timeline: the lock is the truth
    (§5), and a comment on a claimed task is context for its worker, not a
    requeue."""
    if node["number"] in claim_refs:
        return ()
    labels = set(label_names_of(node))
    if "in-progress" in labels:
        return ()
    held = tuple(h for h in REQUEUEABLE_LABELS if h in labels)
    if not held:
        return ()
    return held if requeue_verdict(held, comment_nodes_of(node)) else ()


def read_queue(repo):
    """One queue read: every open kraken-task node (repo-wide, BEFORE any project
    filter) plus every live claim ref. The single fetch both the reconciler (§6)
    and the startable classification consume, so a drain that does both pays for
    it once. Returns (nodes, claim_refs), or None on transport failure."""
    nodes = fetch_open_tasks(repo)
    if nodes is None:
        return None
    claim_refs = claim_ref_list(repo)
    if claim_refs is None:
        return None
    return (nodes, claim_refs)


def classify_queue(repo, project, include_body=False, read=None):
    """The shared startable/held classification list-startable and watch's
    snapshot both read — one code path so the filter can't drift between them.
    Returns a list of (number, title, createdAt, "startable"|"held") ordered
    priority:high-first then oldest-first by createdAt within each tier (a stable
    sort — see PRIORITY_LABEL), or None on transport failure. With
    include_body=True each row
    gains a fifth element, the issue body, so claim-next can brief a subagent
    from the win without a second fetch (the GraphQL walk already has it).

    `read` accepts an already-fetched (nodes, claim_refs) pair from read_queue,
    which is how claim-next classifies the state its reconcile pass just
    produced without re-fetching it.

    Held means: a held label (the projection) OR a live claim ref (the lock).
    Reading both keeps the filter honest across the crash window between a won
    CAS and its label projection — a ref-held task is never offered as
    startable just because the label has not landed yet.

    A held LABEL is not the last word, though: a task whose operator has already
    replied is requeued by derivation (§6, requeued_labels) and rejoins the
    candidates — it still has to clear its dependencies like any other."""
    if read is None:
        read = read_queue(repo)
        if read is None:
            return None
    nodes, claim_refs = read
    project_label = f"project:{project}"
    nodes = [
        n for n in nodes
        if project_label in {l.get("name", "") for l in n.get("labels", {}).get("nodes", [])}
    ]
    # priority:high tasks lead; createdAt breaks ties FIFO within each tier. The
    # key sorts on (not-high, createdAt) so the boolean puts the high tier first
    # and the timestamp keeps the older task ahead inside a tier — a scheduling
    # preference layered on top of pure FIFO (see PRIORITY_LABEL).
    def _is_high(node):
        return PRIORITY_LABEL in {
            l.get("name", "") for l in node.get("labels", {}).get("nodes", [])
        }
    nodes.sort(key=lambda n: (not _is_high(n), n.get("createdAt", "")))

    rows = []            # [number, title, createdAt, state-or-None, body]
    fallback_targets = []  # (row_index, dep_number) needing the depends-on batch

    for node in nodes:
        number = node["number"]
        title = node.get("title", "")
        created = node.get("createdAt", "")
        body = node.get("body") or ""
        label_names = [l.get("name", "") for l in node.get("labels", {}).get("nodes", [])]
        # A held label only holds while the operator has not answered: the
        # requeue derivation lifts the ones an operator reply already cleared.
        still_held = set(HELD_LABELS) - set(requeued_labels(node, claim_refs))
        if any(h in label_names for h in still_held) or number in claim_refs:
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
        for number, title, _, state in rows:  # priority-first, then createdAt FIFO
            if state == "startable":
                print(f"{number}\t{title}")
    return EXIT_OK


# --- subcommand: claim -------------------------------------------------------

def _claim_once(repo, issue, worker, allow_held=()):
    """The one contended claim sequence — guard, CAS, projection — executed
    identically every time (PROTOCOL.md §5). Returns an exit code and prints a
    `claim:` diagnostic line. Shared by `claim` and `claim-next` so they can never
    drift.

    `allow_held` names the held labels the caller's §6 requeue derivation has
    already lifted, so the guard does not refuse the very task the queue filter
    just offered. They are stale projection, and the projection step below swaps
    them off. The guard is an optimisation either way — the claim-ref CAS is the
    only thing that decides ownership."""

    # 1. Guard — re-fetch labels; a held task is skipped with zero writes.
    label_names = issue_label_names(repo, issue)
    if label_names is None:
        print(f"claim: gh-failure issue={issue} stage=guard")
        return EXIT_TRANSPORT
    for held in HELD_LABELS:
        if held in label_names and held not in allow_held:
            print(f"claim: held issue={issue} label={held}")
            return EXIT_NOT_CLEAR
    # The requeued labels still on the issue: dropped as part of the projection.
    stale_held = [h for h in HELD_LABELS if h in label_names and h in allow_held]

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
    for stale in stale_held:
        if not swap_labels(repo, issue, remove=stale):
            print(f"claim: gh-failure issue={issue} stage=label (claim held)")
            return EXIT_TRANSPORT
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

def verify_project(repo, project):
    """Check that the coordination repo actually carries the `project:<name>`
    label this worker was pointed at. Returns (ok, message): True when the label
    exists, False (with a message naming the configured projects and the fix)
    when it does not, and None when the label read itself failed — a project is
    never declared missing from a read that never landed.

    Routing is entirely client-side (§3): a worker scoped to a label nobody uses
    filters every task out and reads as an empty queue forever, so a typo'd or
    never-created project silently produces a worker that hears nothing. Cheap to
    check once, so check it before the drain rather than never."""
    names = list_projects(repo)
    if names is None:
        return (None, "project check: gh-failure stage=labels")
    if project in names:
        return (True, "")
    configured = ", ".join(names) if names else "(none configured)"
    return (False,
            "unknown project: %s has no `project:%s` label, so this worker would "
            "never see a task. Configured projects: %s. Fix the --project "
            "spelling, or create the label with `kraken.py init %s --project %s`."
            % (repo, project, configured, repo, project))


def cmd_claim_next(args):
    """Collapse the deterministic claim loop into one invocation: read the queue,
    reconcile it (§6), then list startable candidates oldest-first and guard + CAS
    each in turn, stopping at the first win. Losses (10/11) move to the next
    candidate; a transport fault (20) stops with state-unknown; an exhausted queue
    is EXIT_NONE. Never retries a lost CAS on the same issue (PROTOCOL.md §5) — it
    iterates forward, never back."""
    repo, project, worker = args.repo, args.project, args.worker

    refused = refuse_second_claim(worker)
    if refused is not None:
        return refused

    # Drift handshake (PROTOCOL.md drift guard): before the first claim, refuse to
    # drain if the coordination repo's vendored .github/kraken.py differs from this
    # worker's bundled copy, or that file cannot be read (fail closed). It stays
    # the FIRST alarm — a drifted sentinel means nothing else read here is
    # trustworthy, so no other diagnostic may mask it.
    ok, message = verify_protocol(repo)
    if not ok:
        print(message)
        return EXIT_PROTOCOL_MISMATCH

    # Project preflight: still before the queue is read and before any write,
    # because a worker scoped to a project label the repo does not carry is deaf,
    # not idle — it would report an honest-looking empty queue forever.
    ok, message = verify_project(repo, project)
    if ok is None:
        print(message)
        return EXIT_TRANSPORT
    if not ok:
        print(message)
        return EXIT_UNKNOWN_PROJECT

    read = read_queue(repo)
    if read is None:
        print("claim-next: gh-failure stage=list")
        return EXIT_TRANSPORT
    nodes, claim_refs = read

    # Reconcile before classifying (PROTOCOL.md §6). A dead worker's claim
    # obstructs exactly one party — the next worker who wants to claim — and that
    # is this one, so the repair belongs here rather than in an hourly cron. The
    # refs are already in hand; only their commit dates are not, so the whole
    # reconcile costs ONE batched read, O(1) in the number of claims, and NOTHING
    # at all when no claim is live. The pass is repo-wide (not project-scoped)
    # because a lock is repo-wide, exactly like the cron it replaces.
    commit_meta = resolve_commit_meta(repo, list(claim_refs.values()))
    if commit_meta is None:
        print("claim-next: gh-failure stage=commits")
        return EXIT_TRANSPORT
    plan = reconcile_plan(nodes, claim_refs, commit_meta, time.time(),
                          reap_max_hours())
    if plan:
        if apply_reconcile(repo, plan, worker) is None:
            # Writes of ours may have half-landed — same state-unknown rule as a
            # failed claim: re-check before retrying, never push on.
            print("claim-next: gh-failure stage=reconcile — state unknown, re-check")
            return EXIT_TRANSPORT
        project_reconcile(plan, nodes, claim_refs)

    rows = classify_queue(repo, project, include_body=True, read=read)
    if rows is None:
        print("claim-next: gh-failure stage=list")
        return EXIT_TRANSPORT

    by_number = {n["number"]: n for n in nodes}
    for number, title, _created, state, body in rows:  # priority-first, then FIFO
        if state != "startable":
            continue
        # Re-derive the requeue verdict from the same node the filter used — a
        # pure call, no request — so the guard accepts a task an operator reply
        # has already requeued instead of refusing what was just offered.
        allow_held = requeued_labels(by_number[number], claim_refs)
        rc = _claim_once(repo, number, worker, allow_held=allow_held)
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


# --- subcommand: note --------------------------------------------------------

def cmd_note(args):
    """Post a free-form worker comment (assumptions, a progress note) with the
    attribution disclaimer prepended and a single non-state-changing `note`
    marker (PROTOCOL.md §4) — the structural signal that makes the §6 requeue
    derivation read it as worker-authored. It carries no machine state: it changes no label and
    touches no claim ref, so the task stays exactly where it was — the missing
    worker-authored write the skill otherwise had you hand-assemble (SKILL.md
    step 2a / Conventions), disclaimer and all."""
    repo, issue, worker, body_file = args.repo, args.issue, args.worker, args.body_file
    if not os.path.isfile(body_file):
        print(f"note: no such file {body_file}", file=sys.stderr)
        return EXIT_USAGE

    prose = read_body_file(body_file)
    if not prose.strip():
        print(f"note: empty body {body_file}", file=sys.stderr)
        return EXIT_USAGE

    body = compose_note(worker, prose)
    if not post_comment(repo, issue, body):
        print(f"note: gh-failure issue={issue} stage=comment")
        return EXIT_TRANSPORT

    print(f"note: posted issue={issue} worker={worker}")
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


# How many consecutive failed queue reads between stderr lines (after the first),
# and how many before the watcher gives up. At the default 60s poll that is: a
# line at once, another every ~10 min while the outage lasts, and death after
# ~1h. Only the ceiling is an operator knob (KRAKEN_WATCH_MAX_FAILURES; 0 keeps
# the watcher alive through any outage, warnings included).
WATCH_WARN_EVERY = 10
WATCH_MAX_FAILURES = 60


def watch_failure_action(failures, warn_every, max_failures):
    """What a run of `failures` consecutive failed queue reads owes the operator:
    "die" once the ceiling is reached, "warn" on the first failure and every
    `warn_every` after it, None in between.

    A watcher whose reads fail is indistinguishable from an idle one — same
    silence, same live process — so a transport fault has to be said out loud,
    but spaced, or an outage would fill the log with one line per poll. Dying
    outranks warning: past the ceiling the watcher is not listening, and a dead
    process in the monitor is honest where a live deaf one is not."""
    if max_failures > 0 and failures >= max_failures:
        return "die"
    if failures <= 0:
        return None
    if failures == 1 or (warn_every > 0 and failures % warn_every == 0):
        return "warn"
    return None


def cmd_watch(args):
    repo, project = args.repo, args.project
    poll_seconds = int(os.environ.get("KRAKEN_WATCH_POLL_SECONDS", "60"))
    retry_seconds = int(os.environ.get("KRAKEN_WATCH_RETRY_SECONDS", "300"))
    max_failures = int(
        os.environ.get("KRAKEN_WATCH_MAX_FAILURES", str(WATCH_MAX_FAILURES))
    )

    # Same preflight as the drain, once, before the first poll: a watcher armed
    # on a project label the repo does not carry can never emit a wake, so it is
    # a silent no-op dressed as an ambush. A label read that merely FAILED is not
    # a refusal — the poll loop already rides out transport faults, so warn and
    # arm rather than killing the watcher over a startup blip.
    ok, message = verify_project(repo, project)
    if ok is False:
        print(message, file=sys.stderr)
        return EXIT_UNKNOWN_PROJECT
    if ok is None:
        print(message + " — arming anyway", file=sys.stderr)

    prev = None
    # Start at "now": retries are owed only for wakes THIS watcher emitted, so
    # a stale flag from an earlier session never triggers one.
    last_emit = time.time()
    failures = 0
    while True:
        snapshot = snapshot_state(repo, project)
        if snapshot is None:
            # Fail loud. The read never landed, so `prev` stays untouched (the
            # edge-triggered gate is not corrupted by an outage) — but silence
            # here is exactly what an idle queue looks like, so say it.
            failures += 1
            action = watch_failure_action(failures, WATCH_WARN_EVERY, max_failures)
            if action == "die":
                print(
                    f"kraken-watch: giving up after {failures} consecutive "
                    f"failures reading the queue in {repo} — this watcher is "
                    f"not listening; check the token and the network",
                    file=sys.stderr, flush=True,
                )
                return EXIT_TRANSPORT
            if action == "warn":
                print(
                    f"kraken-watch: {failures} consecutive failure(s) reading "
                    f"the queue in {repo} — this worker may be offline or "
                    f"unauthenticated, and wakes no one while it is",
                    file=sys.stderr, flush=True,
                )
        else:
            failures = 0
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


_PR_PARTS_RE = re.compile(r"^https?://(?:www\.)?github\.com/([^/]+)/([^/]+)/pull/(\d+)")


def parse_github_pr_url(pr_url):
    """(owner, name, number) parsed from a github.com web pull-request URL, or
    None when the delivery URL isn't one — a non-GitHub delivery (GitLab MR,
    Bitbucket PR, ...) that PROTOCOL.md never forbids, or anything else kraken's
    merge-state check can't understand. Distinct from a transport failure: this
    is a shape check, no network involved."""
    m = _PR_PARTS_RE.search(pr_url or "")
    if m is None:
        return None
    return m.group(1), m.group(2), m.group(3)


def pr_is_merged(pr_url):
    """Whether a delivery PR is already merged — the orphan heuristic's only
    signal. Returns True/False; False also covers a delivery URL kraken cannot
    evaluate (not a github.com pull-request URL) — "not confirmed merged",
    never guessed, so a legitimate non-GitHub delivery no longer bricks the
    whole status read. None is reserved for an actual gh transport failure on a
    github.com PR URL it did understand — compute_status still propagates that
    as the stage=read failure it actually is. The web `…/pull/N` URL recorded
    in the delivery marker maps to the REST `pulls/N` endpoint, whose
    `merged`/`merged_at` fields are the authoritative merge signal."""
    parts = parse_github_pr_url(pr_url)
    if parts is None:
        return False
    owner, name, number = parts
    data = http_json("GET", f"/repos/{owner}/{name}/pulls/{number}")
    if data is None:
        return None
    return (bool(data.get("merged_at")) or bool(data.get("merged"))
            or str(data.get("state", "")).upper() == "MERGED")


def list_projects(repo):
    """Every project:<name> label configured in the repo, sorted, prefix
    stripped — the launch recon points a worker at each. Read from the repo's
    label set (not the open-task walk) so a project with no open task still gets
    a launch line. Returns a sorted name list, or None on transport failure."""
    items = http_paginated(f"/repos/{repo}/labels")
    if items is None:
        return None
    return sorted(
        n["name"][len("project:"):]
        for n in items
        if isinstance(n, dict) and str(n.get("name", "")).startswith("project:")
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


def queue_hygiene(nodes, project=""):
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


def compute_status(repo, project, nodes, now, *, claim_refs, commit_meta,
                   comment_reader, pr_merged, project_lister):
    """Pure-ish status computation, transport injected so it is unit-testable:
    given the queue nodes (from fetch_open_tasks), the claim refs + their commit
    meta, and reader callbacks, build the review/decision/in-flight/projects
    report. Returns the report dict, or None on any injected-transport failure
    (propagated as exit 20)."""
    # Queue hygiene reads the UNFILTERED walk on purpose: the most valuable case
    # it reports — a task carrying no project label at all — is precisely the one
    # a project scope would hide, since the task belongs to no project.
    hygiene = queue_hygiene(nodes, project)
    stale_after = reap_max_hours() * 3600

    if project:
        pl = project
        nodes = [n for n in nodes if pl in project_names_of(n)]

    review, decision, in_flight = [], [], []
    seen_projects = set()

    for node in sorted(nodes, key=lambda n: (n.get("createdAt", ""), n.get("number", 0))):
        seen_projects |= project_names_of(node)
        number = node["number"]
        title = node.get("title", "")
        # The console reads the queue the way a worker does (§6): a task whose
        # operator has already replied is back in the queue, so it must not keep
        # sitting in the review or decision list waiting for a call already made.
        labels = [l for l in label_names_of(node)
                  if l not in requeued_labels(node, claim_refs)]

        if "awaiting-merge" in labels:
            records = comment_reader(repo, number)
            if records is None:
                return None
            pr_url = parse_pr_url(records)
            orphan = False
            merge_state_unknown = False
            if pr_url:
                merged = pr_merged(pr_url)
                if merged is None:
                    return None
                orphan = bool(merged)
                # A non-GitHub delivery (GitLab MR, Bitbucket PR, ...) is never
                # flagged an orphan — pr_merged already returns False for it —
                # but the operator should still see it wasn't actually checked.
                merge_state_unknown = parse_github_pr_url(pr_url) is None
            review.append({"number": number, "title": title, "pr_url": pr_url,
                           "orphan": orphan, "merge_state_unknown": merge_state_unknown})
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
            # Past the staleness threshold this claim is what the next drain's
            # reconcile (§6) will reclaim. Nothing repairs it on a schedule any
            # more, so the console has to say it out loud — otherwise a dead
            # worker looks exactly like a slow one until someone claims.
            in_flight.append({"number": number, "title": title, "worker": worker,
                              "heartbeat_anchor": anchor,
                              "heartbeat_age_seconds": age,
                              "heartbeat_msg": msg,
                              "stale": age is None or age >= stale_after})

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
        "queue_hygiene": hygiene,
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
        if item["orphan"]:
            flag = "  ⚠️  PR looks merged — close it?"
        elif item.get("merge_state_unknown"):
            flag = "  ℹ️  merge-state unknown (non-GitHub delivery)"
        else:
            flag = ""
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
        # A silent claim reads exactly like a busy one, and nothing repairs it on
        # a schedule now, so say which one it is.
        flag = "  ⚠️  silent — the next drain reclaims it" if item.get("stale") else ""
        lines.append(f"     #{item['number']}  {item['title']}  · worker {worker} "
                     f"· last heartbeat {age} ago{note}{flag}")
    lines.append("")

    hygiene = report.get("queue_hygiene") or []
    if hygiene:
        lines.append(f"  🧹 Queue hygiene — {len(hygiene)} task(s) a worker "
                     f"cannot start as filed")
        for item in hygiene:
            lines.append(f"     #{item['number']}  {item['title']}  "
                         f"· missing: {', '.join(item['missing'])}")
        lines.append("     A task with no project: label is invisible to every "
                     "worker; one with no Goal or Acceptance stalls on arrival.")
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

# --- asset drift: the bundled bytes are the single source of truth -----------
# Every bundled asset is meant to be vendored VERBATIM into the coordination repo
# (under `.github/…`), so drift is simply "the vendored copy no longer matches the
# copy this plugin ships". There is no manifest of past release hashes to keep in
# sync — the *current* bundled bytes ARE the reference, compared live, so nothing
# can fall out of date. This covers EVERY installed asset, not just kraken.py:
# cleanup-closed.yml flipped `permissions: contents: read` -> `write` between
# protocol/3 and protocol/4, so a stale workflow is as dangerous as a stale
# parser.


def bundled_asset(name):
    """The raw bytes of a bundled asset shipped in this skill's folder — the
    reference every drift decision compares against."""
    with open(os.path.join(SKILL_DIR, name), "rb") as fh:
        return fh.read()


def classify_asset(current, bundled):
    """Classify a vendored asset against the plugin's bundled copy (a pure
    decision, so it is unit-testable without any network):

      - ``"absent"``    — nothing vendored yet (``current is None``)
      - ``"unchanged"`` — byte-identical to the bundled copy (in sync)
      - ``"drifted"``   — differs from the bundled copy (stale or hand-edited);
                          `init --upgrade` re-syncs it to the bundled bytes
    """
    if current is None:
        return "absent"
    if current == bundled:
        return "unchanged"
    return "drifted"


# --- drift handshake ---------------------------------------------------------
# Before draining, a worker reads the coordination repo's vendored
# `.github/kraken.py` and compares it byte-for-byte with its OWN bundled copy.
# They are meant to be identical — `init` vendors the plugin's copy verbatim — so
# any difference means the plugin and the coordination repo have drifted apart: a
# stale parser (and, by implication, stale workflows) that silently mis-handle the
# protocol. This turns that silent drift into a loud, actionable refusal. One
# cheap contents read, fail closed: an unreadable vendored file refuses rather
# than guessing.
VENDORED_KRAKEN_PATH = ".github/kraken.py"


def verify_protocol(repo):
    """Compare the coordination repo's vendored `.github/kraken.py` with this
    worker's bundled copy. Returns (ok, message): ok is True only when the
    vendored file is readable and byte-identical to the bundled one. A drifted
    file — or an unreadable/absent one (fail closed) — refuses, with a message
    pointing at `init --upgrade`."""
    vendored = gh_get_content(repo, VENDORED_KRAKEN_PATH)
    if vendored is None:
        return (False,
                "drift handshake: cannot verify the coordination repo's vendored "
                "assets — %s is missing or unreadable. Refusing to drain (fail "
                "closed). Run `kraken.py init --upgrade %s` to reinstall the "
                "vendored assets." % (VENDORED_KRAKEN_PATH, repo))
    if vendored != bundled_asset("kraken.py"):
        return (False,
                "drift handshake: %s differs from this worker's bundled kraken.py "
                "(plugin %s) — the coordination repo's vendored assets are stale. "
                "Refusing to drain. Run `kraken.py init --upgrade %s` (or upgrade "
                "this worker's plugin) so both sides agree." % (
                    VENDORED_KRAKEN_PATH, plugin_version(), repo))
    return (True, "")


# Each bundled asset init commits: (bundled filename, destination path in the
# coordination repo, create commit message). ORDER MATTERS: kraken.py is the
# drift-handshake sentinel (verify_protocol reads only it), so it MUST be
# written LAST — init/upgrade aborts on the first failed write, so a partial run
# can never leave the sentinel in sync while a workflow is still stale. Written
# last, an in-sync sentinel proves the whole set synced.
INIT_ASSETS = (
    ("task-template.yml", ".github/ISSUE_TEMPLATE/task.yml",
     "chore: add kraken task template"),
    ("kraken.py", ".github/kraken.py",
     "chore: add kraken transition program"),
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


def gh_repo_exists(repo):
    """True iff the coordination repo already exists (a 2xx from the repo GET)."""
    status, _ = http_request("GET", f"/repos/{repo}")
    return 200 <= status < 300


def gh_repo_create_private(repo):
    """Create the coordination repo PRIVATE — never public: the queue is
    instructions that run in a worker's environment with its credentials. Created
    under the authenticated user (the coordination repo is personal by design)."""
    name = repo.split("/", 1)[1] if "/" in repo else repo
    status, _ = http_request("POST", "/user/repos", {"name": name, "private": True})
    return 200 <= status < 300


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
    obj = http_json("GET", f"/repos/{repo}/contents/{path}")
    if not isinstance(obj, dict):
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
    body = {"message": message, "content": base64.b64encode(data).decode("ascii")}
    if sha is not None:
        body["sha"] = sha
    status, _ = http_request("PUT", f"/repos/{repo}/contents/{path}", body)
    return 200 <= status < 300


def gh_delete_content(repo, path, sha, message):
    """Delete `path` on the repo via the contents API. The current blob `sha` is
    required (same as an update) — GitHub rejects a delete without it, which is
    what keeps the prune from racing a concurrent edit. True on success."""
    status, _ = http_request(
        "DELETE", f"/repos/{repo}/contents/{path}", {"message": message, "sha": sha}
    )
    return 200 <= status < 300


def gh_label_upsert(repo, name, color, description):
    """Upsert a label with its canonical color/description — a create on a fresh
    repo (POST /labels), an in-place re-canonicalize on a re-run (PATCH the
    existing label when the create answers 422 'already_exists'). This is the
    `gh label create --force` behaviour over the REST label surface."""
    body = {"name": name, "color": color, "description": description}
    status, _ = http_request("POST", f"/repos/{repo}/labels", body)
    if status == 422:
        status, _ = http_request(
            "PATCH", f"/repos/{repo}/labels/{quote_path(name)}",
            {"new_name": name, "color": color, "description": description},
        )
    return 200 <= status < 300


def cmd_init(args):
    """Stand up (or repair) a coordination repo: verify-or-create it private,
    install the bundled assets, prune the retired ones, and upsert the canonical
    labels. Plain init is create-only and never overwrites an existing asset — it
    merely REPORTS one as `unchanged` or `drifted` (differs from the bundled
    copy). `--upgrade` additionally re-syncs every `drifted` asset to the bundled
    copy. Re-running init on an older coordination repo is also its MIGRATION:
    assets this protocol revision retired are deleted (OBSOLETE_ASSETS).
    Idempotent; touches no issues. Exit 0 on success, 20 on any gh/transport
    failure."""
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
    #    --upgrade, every asset that has drifted from the bundled copy is re-synced.
    for name, dest, message in INIT_ASSETS:
        try:
            bundled = bundled_asset(name)
        except OSError:
            print(f"init: missing bundled asset {name}", file=sys.stderr)
            return EXIT_USAGE
        current, sha = gh_get_content_meta(repo, dest)
        kind = classify_asset(current, bundled)
        if kind == "absent":
            if not gh_put_content(repo, dest, bundled, message):
                print(f"init: gh-failure stage=asset path={dest}", file=sys.stderr)
                return EXIT_TRANSPORT
            status = "created"
        elif kind == "drifted" and upgrade:
            if not gh_put_content(repo, dest, bundled,
                                  f"chore: upgrade kraken asset {dest}", sha=sha):
                print(f"init: gh-failure stage=asset path={dest}", file=sys.stderr)
                return EXIT_TRANSPORT
            status = "upgraded"
        else:
            # unchanged / drifted (no --upgrade) — write nothing.
            status = kind
        report["assets"].append({"path": dest, "status": status})

    # 3. Prune the assets an earlier release installed and this revision retired,
    #    so re-running init is also the migration path for a coordination repo
    #    that was stood up before them.
    for dest, sentinel in OBSOLETE_ASSETS:
        current, sha = gh_get_content_meta(repo, dest)
        if current is None or sha is None or sentinel not in current:
            continue  # absent, unreadable, or not ours to delete
        if not gh_delete_content(repo, dest, sha,
                                 f"chore: remove retired kraken asset {dest}"):
            print(f"init: gh-failure stage=prune path={dest}", file=sys.stderr)
            return EXIT_TRANSPORT
        report["assets"].append({"path": dest, "status": "removed"})

    # 4. Upsert the canonical labels (+ the project label when scoped).
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
    drifted, upgraded, removed = count("drifted"), count("upgraded"), count("removed")
    lines.append(
        f"init: done repo={report['repo']} repo_status={report['repo_status']} "
        f"assets_created={created} assets_unchanged={unchanged} "
        f"assets_drifted={drifted} assets_upgraded={upgraded} "
        f"assets_removed={removed} labels={len(report['labels'])}"
    )
    # Actionable hint: a plain init only reports drift — point at the repair path.
    if drifted and not report.get("upgrade"):
        lines.append(
            f"init: hint {drifted} asset(s) drifted from the bundled copy — re-run "
            f"`init --upgrade {report['repo']}` to re-sync them")
    return "\n".join(lines)



# --- coordination-repo subcommands -------------------------------------------
# The logic-bearing coordination passes (reconcile, validate-task) live here
# rather than re-implementing the protocol parse in jq/grep/awk — ONE parser,
# sharing the marker decoder, disclaimer, and label vocabulary (and unit tests)
# with the worker side.

# --- the reconciler (PROTOCOL.md §6) -----------------------------------------
# The claim ref is the lock and its commit date the liveness clock, so the refs
# and the in-progress projection have to be made to agree. Under protocol/5 the
# READER does that, not a cron in the coordination repo: a dead worker's claim
# obstructs exactly one party — the next worker who wants to claim — and there is
# no other observer of it, so the reconcile rides the claim path (cmd_claim_next)
# on the queue read it already paid for. `reap` keeps the same pass as an
# operator-side escape hatch; both go through the pure planner below, so the two
# entry points can never drift.

# The default staleness threshold, in hours. A claim ref older than this — or
# unreadable — belongs to a dead worker and is reclaimed to needs-decision.
REAP_DEFAULT_MAX_HOURS = 6

# Who a stand-alone `reap` attributes its comments to when the operator names no
# worker. A drain passes its own worker name instead: under protocol/5 the
# reconciler's comments are posted by a worker's token, so they carry the §4
# attribution disclaimer like every other worker comment.
RECONCILER_WORKER = "reconciler"


def reap_max_hours(explicit=None):
    """The staleness threshold in hours: the explicit value if given, else the
    MAX_HOURS environment override, else the default. A non-integer override
    falls back rather than crashing the pass."""
    if explicit is not None:
        return explicit
    try:
        return int(os.environ.get("MAX_HOURS", REAP_DEFAULT_MAX_HOURS))
    except ValueError:
        return REAP_DEFAULT_MAX_HOURS


def stale_claim_body(worker, reason):
    """The reconciler's reclaim comment: the attribution disclaimer, human prose,
    and the stale-claim marker (audit trail). The disclaimer is required because
    a worker posts this now — under protocol/4 it was the coordination repo's own
    Actions bot, which is why it used to carry none."""
    prose = (
        f"The worker has gone silent ({reason}) and likely died. To requeue, "
        "reply on this thread — or remove the needs-decision label by hand."
    )
    return compose_comment(
        worker, prose, {"type": "stale-claim", "reason": reason}
    )


def orphan_projection_body(worker):
    """The reconciler's requeue note for an in-progress label with no claim ref
    behind it — a crashed release, or a claim made before protocol/4. Same
    authorship and disclaimer rule as stale_claim_body."""
    reason = "in-progress label with no claim ref"
    prose = (
        "This task carried the in-progress label with no live claim ref behind "
        "it (a crashed release, or a claim from before protocol/4). The label "
        "was removed and the task rejoins the queue."
    )
    return compose_comment(worker, prose, {"type": "stale-claim", "reason": reason})


def reconcile_plan(nodes, claim_refs, commit_meta, now, max_hours):
    """The reconciler's decision as a PURE function of one queue read — no
    network, so every rule is unit-testable in isolation (PROTOCOL.md §6):

      1. **orphan lock** — a ref whose issue is not an open task any more, or is
         already labeled needs-decision/awaiting-merge (a terminal transition
         whose ref delete was lost): delete the ref, touch nothing else;
      2. **reclaim** — a ref older than `max_hours`, or whose commit cannot be
         read (nothing proves the worker alive): move the task to
         needs-decision with a stale-claim comment, then delete the ref;
      3. **heal** — a fresh ref on an issue missing its in-progress label (a
         claim whose label projection never landed): add the label;
      4. **requeue** — an open in-progress issue with NO ref (a crashed release,
         or a claim from before protocol/4): remove the label.

    `nodes` is the open-kraken-task walk (repo-wide, before any project filter),
    so rule 1 needs no per-ref issue read: a ref on an issue absent from that
    walk is a ref on an issue that is closed, or no longer a task at all —
    either way it holds a lock over nothing. Nothing on the issue timeline
    anchors liveness, so an operator poking a dead worker's thread shortens
    time-to-triage rather than extending the claim.

    Returns a list of `{"rule", "issue", "reason"}` actions ordered by rule then
    issue number; an empty list means the projection already agrees with the
    locks (the overwhelmingly common case, and it costs zero writes)."""
    open_labels = {n["number"]: set(label_names_of(n)) for n in nodes}
    plan = []

    for num in sorted(claim_refs):
        labels = open_labels.get(num)
        if (labels is None or "needs-decision" in labels
                or "awaiting-merge" in labels):
            plan.append({"rule": "orphan-lock", "issue": num,
                         "reason": "the task already left the claim"})
            continue

        anchor_epoch = parse_iso(
            (commit_meta.get(claim_refs[num]) or {}).get("committedDate") or ""
        )
        if anchor_epoch is None:
            # Nothing proves the worker alive — infinitely stale.
            plan.append({"rule": "reclaim", "issue": num,
                         "reason": "no readable heartbeat on the claim ref",
                         "held": "in-progress" in labels})
            continue
        age_hours = int((now - anchor_epoch) // 3600)
        if age_hours >= max_hours:
            plan.append({"rule": "reclaim", "issue": num,
                         "reason": f"no worker heartbeat for {age_hours}h",
                         "held": "in-progress" in labels})
            continue

        if "in-progress" not in labels:
            plan.append({"rule": "heal", "issue": num,
                         "reason": "in-progress label restored"})

    for num in sorted(open_labels):
        if num in claim_refs or "in-progress" not in open_labels[num]:
            continue
        plan.append({"rule": "requeue", "issue": num,
                     "reason": "in-progress label with no claim ref"})

    return plan


def _reconcile_failure(stage, issue):
    """One failure shape for the applier: name the stage and the issue on stderr,
    then answer None so the caller surfaces exit 20."""
    print(f"reap: gh-failure stage={stage} issue={issue}", file=sys.stderr)
    return None


def apply_reconcile(repo, plan, worker):
    """Execute a reconcile plan. Returns per-rule counts, or None on the first
    transport failure. A half-applied pass is safe: every rule is idempotent and
    ends with the ref delete, so a crash leaves the task HELD and the next
    reader's rule 1 finishes the job — the task is never observably free while a
    reclaim is half-applied (§5's ordering rule)."""
    counts = {"orphan-lock": 0, "reclaim": 0, "heal": 0, "requeue": 0}
    for action in plan:
        rule, num = action["rule"], action["issue"]

        if rule == "orphan-lock":
            if not claim_ref_delete(repo, num):
                return _reconcile_failure("ref", num)
            print(f"reap: orphan-lock issue={num} — claim ref deleted")

        elif rule == "reclaim":
            if not swap_labels(
                repo, num,
                remove="in-progress" if action["held"] else None,
                add="needs-decision",
            ):
                return _reconcile_failure("labels", num)
            if not post_comment(repo, num,
                                stale_claim_body(worker, action["reason"])):
                return _reconcile_failure("comment", num)
            if not claim_ref_delete(repo, num):
                return _reconcile_failure("ref", num)
            print(f"reap: reclaimed issue={num} ({action['reason']})")

        elif rule == "heal":
            if not swap_labels(repo, num, add="in-progress"):
                return _reconcile_failure("heal", num)
            print(f"reap: healed issue={num} — in-progress label restored")

        elif rule == "requeue":
            if not swap_labels(repo, num, remove="in-progress"):
                return _reconcile_failure("requeue", num)
            if not post_comment(repo, num, orphan_projection_body(worker)):
                return _reconcile_failure("comment", num)
            print(f"reap: requeued issue={num} ({action['reason']})")

        counts[rule] += 1
    return counts


def project_reconcile(plan, nodes, claim_refs):
    """Fold an APPLIED plan back into the in-memory queue read, in place, so the
    drain that just reconciled classifies the reconciled state without paying for
    a second fetch. Mirrors exactly what apply_reconcile wrote — nothing more:
    the refs it deleted and the labels it swapped."""
    by_number = {n["number"]: n for n in nodes}
    for action in plan:
        rule, num = action["rule"], action["issue"]
        if rule in ("orphan-lock", "reclaim"):
            claim_refs.pop(num, None)
        node = by_number.get(num)
        if node is None:
            continue
        labels = set(label_names_of(node))
        if rule == "reclaim":
            labels = (labels - {"in-progress"}) | {"needs-decision"}
        elif rule == "heal":
            labels = labels | {"in-progress"}
        elif rule == "requeue":
            labels = labels - {"in-progress"}
        node["labels"] = {"nodes": [{"name": n} for n in sorted(labels)]}


def cmd_reap(args):
    """Run the reconcile pass stand-alone — the operator-side escape hatch for
    the reconcile a drain performs on its own (PROTOCOL.md §6). One queue read
    plus one batched commit-date read, then the plan. Exit 0 on success, 20 on
    any gh/transport failure."""
    repo = args.repo
    read = read_queue(repo)
    if read is None:
        print("reap: gh-failure stage=list", file=sys.stderr)
        return EXIT_TRANSPORT
    nodes, claim_refs = read
    commit_meta = resolve_commit_meta(repo, list(claim_refs.values()))
    if commit_meta is None:
        print("reap: gh-failure stage=commits", file=sys.stderr)
        return EXIT_TRANSPORT

    plan = reconcile_plan(nodes, claim_refs, commit_meta, time.time(),
                          reap_max_hours(args.max_hours))
    counts = apply_reconcile(repo, plan, args.worker)
    if counts is None:
        return EXIT_TRANSPORT

    print(
        f"reap: done refs={len(claim_refs)} reclaimed={counts['reclaim']} "
        f"orphan_locks={counts['orphan-lock']} healed={counts['heal']} "
        f"requeued={counts['requeue']}"
    )
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
    "protocol-version": lambda args: [str(PROTOCOL_VERSION)],
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
        "reap",
        help="run the §6 reconcile stand-alone — reclaim stale claims, delete "
             "orphan locks, heal/requeue projections (a drain does this itself)",
    )
    p.add_argument("repo")
    p.add_argument("worker", nargs="?", default=RECONCILER_WORKER,
                   help="who the reclaim comments are attributed to "
                        f"(default: {RECONCILER_WORKER})")
    p.add_argument("--max-hours", type=int, default=None,
                   help="staleness threshold in hours (default: MAX_HOURS env, else 6)")
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
    p.add_argument("--upgrade", action="store_true",
                   help="re-sync every vendored asset that has drifted from the "
                        "bundled copy (the repair path after a plugin upgrade)")
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
