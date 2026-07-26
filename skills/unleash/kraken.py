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

Claiming (kraken-protocol/7) is a true compare-and-swap: creating the claim
ref `refs/kraken/claims/<issue>/<generation>` succeeds for exactly one caller —
GitHub answers HTTP 422 to everyone else — so the ref IS the arbiter, and its
commit's server-stamped date IS the lease timestamp. The ref is a LEASE, not an
eternal lock: past its TTL it holds nothing, and whoever READS the queue applies
that. The holder is whoever owns the HIGHEST generation, and the only way to
become the holder is to create the next one — so a first claim, a steal and a
renewal are all the same conflict-failing create on the same ref name, nothing
is ever deleted to make room, and exactly one of them can win. Recovery from a
dead worker therefore takes minutes, in every harness, with nothing installed.
The retired protocol/3 comment
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
costs O(pages), not one REST call per non-held task. Comment threads are NOT on
that walk — they are hydrated for the held/leased handful that needs them
(hydrate_comment_windows), so poll payload scales with what is blocked rather
than with the whole queue.

Exit-code contract (PROTOCOL.md §12), preserved verbatim from the scripts:
    0   success
    10  lost the claim CAS — another worker holds the claim ref; back off
        (writing nothing) and pick the next candidate
    11  no longer clear — a held label appeared since listing; skip the task
    20  gh / network transport failure — state unknown, re-check before retry
    3   claim-next only: no candidate was startable — the queue is empty, or
        every candidate turned out held/lost as it iterated (nothing to claim,
        an honest empty result, not a fault)
    2   bad invocation (missing file / unknown mode)
"""

import argparse
import base64
import contextlib
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
EXIT_UNKNOWN_PROJECT = 13  # drain refused: the repo has no project:<name> label
EXIT_TRANSPORT = 20
EXIT_NONE = 3  # claim-next: nothing startable to claim (not empty-vs-error ambiguous)
EXIT_USAGE = 2

# A task carrying either of these is held, never startable. Both are
# operator-facing states with no lock behind them, which is exactly why a label
# can decide them: the label IS the state.
#
# `in-progress` is deliberately absent. It is the claim ref's projection, and a
# projection read by a decision is a second source of truth that then has to be
# reconciled with the first. Under protocol/7 it is WRITE-ONLY: written for the
# human reading the issue list, never consulted by the claim guard, the startable
# filter, the requeue derivation or the console. The lease alone says whether
# somebody is working, so a label that lags it costs nothing but a stale badge.
HELD_LABELS = ("needs-decision", "awaiting-merge")

# A scheduling preference, not a state: a startable task carrying this label is
# offered ahead of normal ones (createdAt FIFO still breaks ties within each
# tier). Not a coordination invariant — an old worker that ignores it simply
# falls back to pure FIFO, so honoring it needs no PROTOCOL_VERSION bump.
PRIORITY_LABEL = "priority:high"

# The wire contract this program speaks (PROTOCOL.md): protocol/4 arbitrates the
# claim on a git-ref CAS and anchors liveness to the claim ref's commit date;
# protocol/5 moves the reconcile of that clock onto the READER (§6), so a
# conforming coordination repo runs no scheduled job of its own; protocol/6 gives
# that clock an expiry — the claim ref is a LEASE, and a reader that finds one
# older than the TTL steals it instead of treating it as held; protocol/7 makes
# the `in-progress` label WRITE-ONLY, so the lease is the only thing any decision
# reads and the two can no longer disagree.
PROTOCOL_VERSION = 7

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


# --- diagnostic output -------------------------------------------------------
# Every transition prints one-line diagnostics a human (or a shell) reads off
# stdout: "claim: held issue=7 label=needs-decision", "reap: reclaimed issue=9 …".
# `next-action` (below) owns stdout for its JSON envelope, so it routes those
# same lines to stderr for the duration of its run — identical text, identical
# order, just off the machine channel. Only the functions next-action reaches
# through (the claim path and the reconcile applier) go through `diag`; a
# subcommand that never runs inside it keeps printing directly.

_DIAG_STREAM = None  # None -> sys.stdout, resolved per call so tests can capture


def diag(text):
    """Print one diagnostic line to the current diagnostic sink."""
    print(text, file=_DIAG_STREAM if _DIAG_STREAM is not None else sys.stdout)


@contextlib.contextmanager
def diagnostics_on_stderr():
    """Route `diag` to stderr while stdout carries a machine payload."""
    global _DIAG_STREAM
    previous = _DIAG_STREAM
    _DIAG_STREAM = sys.stderr
    try:
        yield
    finally:
        _DIAG_STREAM = previous


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
# exists only so the comment is recognizable as worker-authored (§4).
# `lease-expired` heads the steal's audit comment and is also COUNTED (§6's
# repeat-expiry guard), which is why it is a type of its own rather than a
# `stale-claim` reason. (`requeue` is operator-only.) The lint checks each
# against PROTOCOL.md's marker table via `kraken.py contract marker-types`.
MARKER_TYPES = ("claim", "heartbeat", "needs-decision", "delivered",
                "released", "stale-claim", "lease-expired", "note")


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


# --- claim refs: the CAS ladder, and the lease it carries --------------------
#
# The claim of issue N is a git ref refs/kraken/claims/N/<generation>. Creating a
# ref is the one common GitHub write that FAILS on conflict (422 to all but one
# creator), so the ref is the arbiter and the loser writes nothing. It points at
# an orphan commit whose message is the kraken marker and whose server-stamped
# date is the LEASE TIMESTAMP (§5): the ref does not hold forever — a reader that
# finds it older than the TTL treats it as expired.
#
# THE GENERATION IS WHAT MAKES THE STEAL A REAL CAS. The holder of a task is
# whoever owns its HIGHEST generation, and the only way to become the holder is
# to CREATE the next one. So every contended operation — first claim, steal,
# renewal — is the same conflict-failing primitive, racing on the same ref name:
#
#   free task            -> create N/1
#   steal an expired G   -> create N/(G+1)
#   renew your own G     -> create N/(G+1), then drop G
#
# Nothing is ever deleted to make room, which is what removes the last window in
# the design: a thief and the live holder renewing race on the identical ref, the
# server picks one, and the loser is told. Deleting a superseded generation is
# garbage collection — losing that delete costs a stray ref, never a lease.
#
# Refs are UI-invisible, which is the whole reason the in-progress label exists:
# it is written after the CAS so a human scanning the issue list can see the ref
# they cannot. Nothing reads it back (§3).

CLAIM_REF_PREFIX = "refs/kraken/claims/"
# Generation of a bare refs/kraken/claims/<issue> — the shape protocol/5 wrote.
# It is never created any more, only read and superseded: an old ref is simply
# the lowest possible generation, so a protocol/5 claim is stolen by creating
# generation 1 over it, with no migration step.
LEGACY_CLAIM_GEN = 0
# git's well-known empty-tree object, present in every repo, so an orphan commit
# needs no prior read; create_claim_commit falls back to HEAD's tree if a host
# rejects it.
EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def claim_ref(issue, gen):
    if gen == LEGACY_CLAIM_GEN:
        return f"{CLAIM_REF_PREFIX}{issue}"
    return f"{CLAIM_REF_PREFIX}{issue}/{gen}"


def parse_claim_ref(ref):
    """`(issue, generation)` for a claim ref name, or None for anything else.

    Both shapes are accepted: `…/claims/12` is the protocol/5 ref, read as
    generation 0, and `…/claims/12/3` is generation 3. Strict parsing matters
    because GitHub's matching-refs is a plain PREFIX match — asking for
    `kraken/claims/12` also returns `kraken/claims/120/1` — so the caller filters
    on what this returns, never on the prefix it asked for."""
    if not ref.startswith(CLAIM_REF_PREFIX):
        return None
    parts = ref[len(CLAIM_REF_PREFIX):].split("/")
    if len(parts) == 1 and parts[0].isdigit():
        return (int(parts[0]), LEGACY_CLAIM_GEN)
    if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
        return (int(parts[0]), int(parts[1]))
    return None


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


def claim_ref_create(repo, issue, gen, sha):
    """The CAS itself: create generation `gen` of issue `issue`'s claim ref.
    Returns "won" (created — this worker is now the holder), "lost" (HTTP 422:
    somebody else created this generation first), or "fail" (transport — state
    unknown). The verdict is the integer status code, nothing else.

    This one call arbitrates every contended operation: a first claim (gen 1), a
    steal (holder's gen + 1) and a renewal (own gen + 1) all compete here, so a
    thief and the holder it is stealing from race on the SAME ref name and the
    server picks exactly one."""
    status, _text = http_request(
        "POST", f"/repos/{repo}/git/refs",
        {"ref": claim_ref(issue, gen), "sha": sha},
    )
    if 200 <= status < 300:
        return "won"
    if status == 422:
        return "lost"
    return "fail"


def claim_ref_delete(repo, issue, gen):
    """Delete one generation of a claim ref. An already-missing ref (HTTP 422)
    counts as success: it is gone either way, and the delete stays idempotent
    under retries."""
    status, _text = http_request(
        "DELETE", f"/repos/{repo}/git/{claim_ref(issue, gen)}"
    )
    return 200 <= status < 300 or status == 422


def drop_generations(repo, issue, gens):
    """Delete a set of generations — how a lease is released (every generation
    of it) and how a superseded one is collected after an advance. True only if
    all of them went; a leftover generation is untidy, never a held lease,
    because the holder is decided by the HIGHEST generation."""
    ok = True
    for gen in gens:
        if not claim_ref_delete(repo, issue, gen):
            ok = False
    return ok


def _parse_ref_items(items):
    """[(issue, gen, sha)] out of a matching-refs payload, dropping anything
    that is not a claim ref — matching-refs is a prefix match, so the filter is
    on the parsed name, never on the prefix that was asked for."""
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        sha = (item.get("object") or {}).get("sha") or ""
        parsed = parse_claim_ref(item.get("ref") or "")
        if sha and parsed is not None:
            out.append((parsed[0], parsed[1], sha))
    return out


def claim_ref_list(repo):
    """Every live claim ref as {issue_number: [(generation, sha), …]}, in one
    paginated matching-refs read. Returns a dict (empty when none), or None on
    transport failure. Generations are not collapsed here: the holder is the
    highest one, and the rest are the superseded refs a reader may collect."""
    items = http_paginated(f"/repos/{repo}/git/matching-refs/kraken/claims/")
    if items is None:
        return None
    refs = {}
    for issue, gen, sha in _parse_ref_items(items):
        refs.setdefault(issue, []).append((gen, sha))
    return refs


def claim_refs_of(repo, issue):
    """One issue's claim refs as `(ok, [(generation, sha), …])` — the sorted
    generation ladder, empty when the task is unclaimed. `ok` is False on a
    transport failure, and only then, so 'nobody holds it' is never confused
    with 'the read did not land'."""
    if not str(issue).lstrip("-").isdigit():
        return (False, [])
    items = http_paginated(
        f"/repos/{repo}/git/matching-refs/kraken/claims/{int(issue)}"
    )
    if items is None:
        return (False, [])
    return (True, sorted((gen, sha) for i, gen, sha in _parse_ref_items(items)
                         if i == int(issue)))


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


def claim_ref_head(repo, issue):
    """The head of one issue's lease — the read every ownership question goes
    through: which generation holds it, whose it is, and since when.

    Returns `(state, head)`, and the three states are kept apart on purpose
    because they demand different answers:
      - `("ok", {"gen", "sha", "worker", "epoch", "gens"})` — the highest
        generation, plus `gens`, every generation present (so a caller that is
        releasing knows what to delete and one that is advancing knows what to
        collect);
      - `("absent", None)` — no claim ref at all, the task is unclaimed;
      - `("error", None)` — the read did not land, so the answer is *unknown*
        and no caller may write on the strength of it.
    `epoch` is None when the commit carries no readable date — the same
    fail-open the queue-wide read applies: nothing proves the holder alive."""
    ok, refs = claim_refs_of(repo, issue)
    if not ok:
        return ("error", None)
    if not refs:
        return ("absent", None)
    gen, sha = refs[-1]  # sorted: the highest generation is the holder
    meta = resolve_commit_meta(repo, [sha])
    if meta is None:
        return ("error", None)
    entry = meta.get(sha) or {}
    payload = parse_marker(entry.get("message") or "") or {}
    return ("ok", {
        "gen": gen,
        "sha": sha,
        "worker": payload.get("worker") or None,
        "epoch": parse_iso(entry.get("committedDate") or ""),
        "gens": [g for g, _s in refs],
    })


def advance_lease(repo, issue, gen, payload):
    """Take (or keep) the lease by creating the generation ABOVE `gen` — the one
    contended write, shared by the steal and the renewal (PROTOCOL.md §5.2).

    Returns `(verdict, gen, sha)`: "won" with the new generation and its commit,
    "lost" when another worker created that generation first, or "fail-commit" /
    "fail-ref" on transport — the two failures stay apart because a caller's
    diagnostic names the stage that actually broke. Nothing is deleted here, so
    there is no instant at which the task is unheld and no way for two workers to
    both believe they took it: they are competing to create one identical ref
    name, and the server admits one."""
    sha = create_claim_commit(repo, payload)
    if sha is None:
        return ("fail-commit", None, None)
    verdict = claim_ref_create(repo, issue, gen + 1, sha)
    if verdict == "fail":
        return ("fail-ref", None, None)
    return (verdict, gen + 1 if verdict == "won" else None,
            sha if verdict == "won" else None)


def claim_ref_owner(repo, issue):
    """The worker named in the claim commit the ref for `issue` currently points
    at, or None when the ref is absent or unreadable. This is how a lost CAS
    (HTTP 422) is told apart: a 422 is a genuine loss only when the ref belongs
    to a DIFFERENT worker; a worker re-claiming its OWN in-flight claim after a
    network failure already owns the task (PROTOCOL.md §5's re-check caveat).
    None (transport/absent) is treated by the caller as 'not mine', so an
    ambiguous read never turns a real loss into a false win."""
    state, head = claim_ref_head(repo, issue)
    return (head or {}).get("worker") if state == "ok" else None


# --- the lease: how long a claim ref holds -----------------------------------
#
# Through protocol/5 the claim ref was a lock that never expired, and repairing a
# dead worker's claim was a threshold measured in HOURS. Under protocol/6 it is a
# LEASE: the ref's commit date is the lease timestamp, the TTL below is how long
# that timestamp holds, and the party that applies the expiry is the READER —
# no scheduled job, no new state, no extra request (the date is already in hand
# from the queue read). Recovery from a dead worker therefore takes at most one
# TTL, in every harness, with nothing installed.

# The lease TTL in seconds. Minutes, not hours: this is the worst-case time a
# dead worker's task sits unavailable. It is bounded below by what a LIVE worker
# can honour — a worker renews every TTL/3, so the TTL must comfortably span a
# long silent step (a build, a test suite, one slow tool call) or a live worker
# would lose a lease it is still working under.
LEASE_DEFAULT_TTL_SECONDS = 1800  # 30 minutes

# Renewal cadence is DERIVED, never configured separately: a worker renews every
# TTL/3, so two consecutive renewals may be lost before anyone steals the lease.
LEASE_RENEW_DIVISOR = 3

# How many expiries on ONE task turn the steal into an escalation. A task that
# kills every worker that touches it would otherwise circulate forever, each
# steal burning another drain; past this count the operator hears about it
# instead (§6). Counted from the `lease-expired` markers already in the queue
# read's comment window, so the guard costs nothing.
LEASE_EXPIRY_ESCALATE = 3


def lease_ttl_seconds(explicit=None):
    """The lease TTL in seconds: the explicit value if given, else the
    KRAKEN_LEASE_TTL_SECONDS environment override, else the default. A
    non-integer or non-positive override falls back rather than crashing the
    pass — or worse, expiring every live lease at once."""
    if explicit is not None:
        return explicit
    try:
        ttl = int(os.environ.get("KRAKEN_LEASE_TTL_SECONDS", LEASE_DEFAULT_TTL_SECONDS))
    except ValueError:
        return LEASE_DEFAULT_TTL_SECONDS
    return ttl if ttl > 0 else LEASE_DEFAULT_TTL_SECONDS


def lease_renew_seconds(ttl=None):
    """How often a worker holding a lease must renew it: TTL/3."""
    return (lease_ttl_seconds() if ttl is None else ttl) // LEASE_RENEW_DIVISOR


def lease_state(claim_refs, commit_meta, now, ttl):
    """The lease view of a queue read, as a PURE function — the one place expiry
    is decided, so every reader (the startable filter, the reconciler, the claim
    path, the status console) applies the same clock.

    `claim_refs` is {issue: [(generation, sha), …]} — the holder is the HIGHEST
    generation, and the lower ones are superseded refs a reader may collect.
    Returns {issue: {"gen", "sha", "worker", "epoch", "age", "live", "gens"}}. A
    ref whose commit date cannot be read has `epoch`/`age` None and is **not
    live**: nothing proves the holder alive, so the lease fails OPEN to the steal
    rather than holding the task forever behind an unreadable clock."""
    leases = {}
    for issue, refs in claim_refs.items():
        gen, sha = max(refs)
        entry = commit_meta.get(sha) or {}
        epoch = parse_iso(entry.get("committedDate") or "")
        payload = parse_marker(entry.get("message") or "") or {}
        age = None if epoch is None else max(0, int(now - epoch))
        leases[issue] = {
            "gen": gen,
            "sha": sha,
            "worker": payload.get("worker") or None,
            "epoch": epoch,
            "age": age,
            "live": age is not None and age < ttl,
            "gens": sorted(g for g, _s in refs),
        }
    return leases


def holder_shas(claim_refs):
    """Only the SHAs the lease clock is read from — the highest generation of
    each issue. A superseded generation's commit date decides nothing, so the
    batched commit read stays one entry per claimed task, not per ref."""
    return [max(refs)[1] for refs in claim_refs.values() if refs]


def live_leases(leases):
    """Just the still-held leases, in the {issue: sha} shape the readers that
    only ask "is this task locked?" already consume."""
    return {issue: l["sha"] for issue, l in leases.items() if l["live"]}


def hold_lease(command, repo, issue, worker):
    """The write-after-expiry check (PROTOCOL.md §5.3): prove this worker still
    holds the lease BEFORE any write of a transition. Returns `(code, head)` —
    `code` None when the lease is ours and the caller may proceed (with `head`
    naming the generations to release), otherwise the exit code to return.

    Ownership is the HIGHEST generation: a thief takes the lease by creating the
    one above ours, so our own ref still existing proves nothing — what proves it
    is that nobody has climbed past us.

    This is what makes a short TTL safe. A worker that stalled long enough to be
    stolen from — a suspended laptop, a rate-limit wait, a very long build — is
    otherwise indistinguishable from a live one until it tries to deliver, and a
    delivery written onto a task somebody else is now executing is worse than no
    delivery at all. So: not ours, or gone, means write NOTHING and exit 10; an
    ambiguous read means exit 20 and re-check, never a write on a guess.

    A lease that is ours but past its TTL still passes: it expired, but nobody
    took it, and finishing the transition frees it honestly — the check is about
    who holds the lease, not how old it is."""
    state, head = claim_ref_head(repo, issue)
    if state == "error":
        print(f"{command}: gh-failure issue={issue} stage=lease")
        return (EXIT_TRANSPORT, None)
    if state == "absent":
        print(f"{command}: lost-lease issue={issue} — the lease is gone, "
              "re-claim the task before writing to it")
        return (EXIT_LOST, None)
    if head["worker"] != worker:
        holder = head["worker"] or "another worker"
        print(f"{command}: lost-lease issue={issue} — the lease is held by {holder}")
        return (EXIT_LOST, None)
    return (None, head)


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


def open_claim_record(worker):
    """The whole claim-<worker>.json record — `{"repo", "issue", "worker"}` with
    `issue` normalized to a string — or None when this worker holds no open
    claim. The file's *presence* is the signal that a claim is unresolved: every
    terminal transition (deliver / escalate / release) removes it, so a resolved
    claim leaves nothing behind. A missing, unreadable, or malformed file is
    treated as no open claim — the guards it feeds must never fail a claim over
    an unparseable scratch file (the lease backs us up regardless).

    `next-action` reads the record rather than the bare issue number, because
    resuming a claim means proving the lease on the repo the claim was made in,
    not on whichever repo this invocation happens to name."""
    try:
        with open(claim_state_path(worker), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    issue = data.get("issue")
    if issue is None:
        return None
    return {
        "repo": str(data.get("repo") or ""),
        "issue": str(issue),
        "worker": str(data.get("worker") or worker),
    }


def open_claim(worker):
    """The issue number (as a string) of an open claim this worker still holds,
    or None when there is none — the one field the one-task-at-a-time guard
    needs, read off open_claim_record so there is a single parse of the file."""
    record = open_claim_record(worker)
    return None if record is None else record["issue"]


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
    diag(
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

# How many trailing comments a HYDRATED task carries. The requeue derivation (§6)
# only ever needs the NEWEST comment of two classes — the last worker comment and
# any operator reply after it — so a window cannot mislead it: a window holding no
# worker comment means the last one is older than the window, which makes every
# operator comment inside it newer, the correct verdict. 25 is therefore about
# payload, not correctness.
QUEUE_COMMENT_WINDOW = 25

# How many issues one hydration call asks about. The window is per issue, so the
# chunk bounds the node count of a single query rather than the number of queries
# — 50 × 25 keeps a hydration page about the size of a queue page.
COMMENT_HYDRATE_CHUNK = 50


def fetch_open_tasks(repo):
    """Every OPEN kraken-task issue in the repo, across all projects — number,
    title, createdAt, body, labels and native blocked-by — in one paginated
    GraphQL walk. Returns the node list, or None on transport failure.

    Deliberately does NOT carry comments. This walk is the hot read: every
    worker's watcher runs it once a minute, so a trailing comment window on every
    node was the queue's first scaling wall — a 200-task queue shipped 5,000
    comment bodies per poll, per worker, to answer a question about a handful of
    them. Both readers that need comments (the requeue derivation and
    `expiry_count`) only ever ask about tasks that are HELD or LEASED, so the
    window is fetched for exactly those — see `hydrate_comment_windows`."""
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


def fetch_comment_windows(repo, numbers):
    """The trailing comment window of the named issues, `{number: [nodes]}`, in
    batched aliased GraphQL calls (`iN: issue(number: N) { comments(last: …) }`)
    — the same shape `resolve_depends_on` uses, for the same reason: a fan-out
    the queue walk no longer pays for on every task must not become one request
    per task either. Returns None on transport failure.

    Cost is O(len(numbers) / COMMENT_HYDRATE_CHUNK) calls, and O(1) — no call at
    all — for the overwhelmingly common empty ask."""
    if not numbers:
        return {}
    owner, name = repo.split("/", 1)
    numbers = sorted(numbers)
    out = {}
    for start in range(0, len(numbers), COMMENT_HYDRATE_CHUNK):
        chunk = numbers[start:start + COMMENT_HYDRATE_CHUNK]
        fields = " ".join(
            f"i{n}: issue(number: {n}) {{ comments(last: {QUEUE_COMMENT_WINDOW}) "
            f"{{ nodes {{ body createdAt }} }} }}"
            for n in chunk
        )
        resp = graphql(f'{{ repository(owner: "{owner}", name: "{name}") {{ {fields} }} }}')
        if resp is None:
            return None
        repo_obj = resp["data"]["repository"]
        for n in chunk:
            obj = repo_obj.get(f"i{n}") or {}
            out[n] = ((obj.get("comments") or {}).get("nodes")) or []
    return out


# --- the requeue derivation (PROTOCOL.md §6) ---------------------------------
# When the operator answers a held task, the task rejoins the queue. Through
# protocol/4 that was a MUTATION: a workflow watched the comment stream and
# removed the holding label. But "an operator replied after the last worker
# comment" is a property of the thread, readable at any time — so protocol/5
# DERIVES it on the read instead. No workflow, no label write, and no window
# where the queue disagrees with the thread.
#
# The derivation reads a trailing comment window (QUEUE_COMMENT_WINDOW). It used
# to ride the queue walk, which made it free per task but charged every task for
# it; it is now hydrated for the held/leased handful instead (see
# hydrate_comment_windows), which is the same free on an idle queue and O(held)
# rather than O(queue) on a busy one.

# The labels a reply can lift are exactly the labels that hold (HELD_LABELS):
# both are operator-facing states, and answering one is what ends it. Under
# protocol/6 this was a second, narrower tuple because `in-progress` held without
# being requeueable; protocol/7 made that label write-only, so the two sets
# coincide and one name is enough.


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
    """A task node's trailing comment window, oldest-first — empty for a node the
    hydration pass did not fill.

    That empty answer is not a guess. `hydrate_comment_windows` fills exactly the
    nodes the two comment readers can be called on — `requeued_labels` only reads
    a node wearing a held label with no live lease, `expiry_count` only a node
    whose lease expired — so an unhydrated node is one neither reader visits, and
    the emptiness is unobservable rather than merely harmless."""
    return (node.get("comments") or {}).get("nodes") or []


def comment_hungry(nodes, leases):
    """The issue numbers whose comment window a read of this queue actually needs
    — the set `hydrate_comment_windows` fetches, as a pure function so the
    fetch's scope is testable without transport.

    Exactly two readers consume comments, and each names its own tasks:

      1. the requeue derivation (§6) — a task wearing a HELD label. A live lease
         outranks the thread (`requeued_labels` returns early), so a locked task
         is not hungry however it is labelled;
      2. `expiry_count` (§6 rule 2) — a task whose lease EXPIRED, which the
         reconciler counts steals on before escalating. A live lease is not
         reconciled at all, and a ref on an issue outside the walk is an orphan
         lock deleted without ever reading a comment.

    Everything else in the queue — the startable tasks, which is nearly all of it
    — is decided from labels, blocked-by and body alone."""
    live = live_leases(leases)
    held_labels = set(HELD_LABELS)
    hungry = set()
    for node in nodes:
        number = node["number"]
        if number in live:
            continue
        if label_names_of(node) & held_labels:
            hungry.add(number)
    walked = {node["number"] for node in nodes}
    for number, lease in leases.items():
        if not lease["live"] and number in walked:
            hungry.add(number)
    return hungry


def hydrate_comment_windows(repo, nodes, leases):
    """Attach the trailing comment window to the queue nodes that need one, in
    place, and answer the nodes back — or None on transport failure, which the
    callers propagate exactly like a failed walk.

    This is the second half of the split that took the comment window off the hot
    read (see fetch_open_tasks): the walk answers "what is in the queue" for every
    task, and this answers "what does the thread say" for the few whose answer
    depends on it. An idle queue hydrates nothing and pays nothing."""
    hungry = comment_hungry(nodes, leases)
    windows = fetch_comment_windows(repo, hungry)
    if windows is None:
        return None
    for node in nodes:
        window = windows.get(node["number"])
        if window is not None:
            node["comments"] = {"nodes": window}
    return nodes


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


def requeued_labels(node, live):
    """The held labels this task carries that an operator reply has already
    lifted — a tuple, empty when the task is genuinely held. Pure, so the
    classification and the claim that follows it reach the same verdict from the
    same node without a second read.

    A **live lease** outranks any reading of the timeline: the lock is the truth
    (§5), and a comment on a claimed task is context for its worker, not a
    requeue. An EXPIRED lease outranks nothing — it holds no task (§6), so
    `live` is the live-lease view, never the raw ref list. The lease is the ONLY
    thing that outranks the thread here: `in-progress` is write-only (§3), so a
    stale badge left on a task cannot suppress a requeue the operator earned."""
    if node["number"] in live:
        return ()
    labels = set(label_names_of(node))
    held = tuple(h for h in HELD_LABELS if h in labels)
    if not held:
        return ()
    return held if requeue_verdict(held, comment_nodes_of(node)) else ()


def read_queue(repo, now=None, ttl=None):
    """One queue read: every open kraken-task node (repo-wide, BEFORE any project
    filter) plus the LEASE state of every claim ref. The single fetch the
    reconciler (§6), the startable classification and the claim path all consume,
    so a drain that does all three pays for it once. Returns (nodes, leases), or
    None on transport failure.

    Resolving the lease costs one extra batched call — and only when a ref
    actually exists: `resolve_commit_meta` answers `{}` for an empty ref list
    without a request, so an idle queue reads exactly as cheaply as it did under
    protocol/5, and a busy one pays O(1) in the number of claims, never O(N).

    The comment hydration obeys the same rule and for the same reason: the leases
    have to be known before the hungry set can be (a live lease outranks the
    thread), so it runs last, and it asks for nothing when nothing is held."""
    nodes = fetch_open_tasks(repo)
    if nodes is None:
        return None
    claim_refs = claim_ref_list(repo)
    if claim_refs is None:
        return None
    commit_meta = resolve_commit_meta(repo, holder_shas(claim_refs))
    if commit_meta is None:
        return None
    leases = lease_state(claim_refs, commit_meta,
                         time.time() if now is None else now,
                         lease_ttl_seconds(ttl))
    if hydrate_comment_windows(repo, nodes, leases) is None:
        return None
    return (nodes, leases)


def classify_queue(repo, project, include_body=False, read=None):
    """The shared startable/held classification list-startable and watch's
    snapshot both read — one code path so the filter can't drift between them.
    Returns a list of (number, title, createdAt, "startable"|"held") ordered
    priority:high-first then oldest-first by createdAt within each tier (a stable
    sort — see PRIORITY_LABEL), or None on transport failure. With
    include_body=True each row
    gains a fifth element, the issue body, so claim-next can brief a subagent
    from the win without a second fetch (the GraphQL walk already has it).

    `read` accepts an already-fetched (nodes, leases) pair from read_queue,
    which is how claim-next classifies the state its reconcile pass just
    produced without re-fetching it.

    Held means: a held label (needs-decision / awaiting-merge — an operator-facing
    state) OR a **live lease** (the lock). Those are the only two kinds of hold
    there are, and they do not overlap: `in-progress` is write-only (§3), so the
    lease answers for it and a task wearing a stale badge with no live lease is
    offered like any other. An EXPIRED lease holds nothing either (§5): the task
    is offered, and the claim that follows steals the lease.

    A held LABEL is not the last word, though: a task whose operator has already
    replied is requeued by derivation (§6, requeued_labels) and rejoins the
    candidates — it still has to clear its dependencies like any other."""
    if read is None:
        read = read_queue(repo)
        if read is None:
            return None
    nodes, leases = read
    live = live_leases(leases)
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
        still_held = set(HELD_LABELS) - set(requeued_labels(node, live))
        if any(h in label_names for h in still_held) or number in live:
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

def lease_expired_body(worker, stale):
    """The thief's audit comment: who held the lease, how long it had been
    silent, and who took it. Two jobs — it is the only human-readable trace that
    a task changed hands, and its `lease-expired` marker is what §6's repeat
    guard counts, so one comment per steal, never one per read."""
    previous = stale.get("worker") or "an unnamed worker"
    age = stale.get("age")
    silence = "its clock could not be read" if age is None \
        else f"it had been silent for {format_age(age)}"
    prose = (
        f"The lease held by `{previous}` expired ({silence}), so this task was "
        f"taken over. If `{previous}` is still alive it will find the lease gone "
        "and stop before writing anything."
    )
    payload = {"type": "lease-expired", "worker": worker,
               "previous_worker": stale.get("worker") or ""}
    if age is not None:
        payload["age_seconds"] = age
    return compose_comment(worker, prose, payload)


def probe_lease_state(repo, issue, ttl):
    """Read one issue's lease into the same record shape a queue read produces —
    `{"gen", "sha", "worker", "age", "live", "gens"}` — or None when the task
    carries no claim ref at all. `False` (the sentinel) on a transport failure,
    so 'unclaimed' is never confused with 'unknown'.

    A drain never needs this: the queue read already carries every lease. It is
    the named-claim path's equivalent, and a slightly stale answer is safe by
    construction — the CAS that follows competes by CREATING the generation above
    the one this saw, so a holder that renewed in the meantime already took that
    generation and the challenger simply loses. This read decides whether to
    *try*, never who wins."""
    state, head = claim_ref_head(repo, issue)
    if state == "error":
        return False
    if state == "absent":
        return None
    age = None if head["epoch"] is None else max(0, int(time.time() - head["epoch"]))
    return {"gen": head["gen"], "sha": head["sha"], "worker": head["worker"],
            "age": age, "live": age is not None and age < ttl,
            "gens": head["gens"]}


def _claim_once(repo, issue, worker, allow_held=(), lease=None, ttl=None,
                probe_lease=False):
    """The one contended claim sequence — guard, CAS, projection — executed
    identically every time (PROTOCOL.md §5). Returns an exit code and prints a
    `claim:` diagnostic line. Shared by `claim` and `claim-next` so they can never
    drift.

    `allow_held` names the held labels the caller's §6 requeue derivation has
    already lifted, so the guard does not refuse the very task the queue filter
    just offered. They are stale projection, and the projection step below swaps
    them off. The guard is an optimisation either way — the claim-ref CAS is the
    only thing that decides ownership.

    `lease` is the lease record the caller observed on this issue, or None when
    it observed no claim ref. It decides the generation the CAS starts from — a
    free task takes generation 1, and a task whose lease expired takes the one
    ABOVE its holder. That is the whole of what a "steal" is: not a different
    algorithm, a different starting rung.

    `probe_lease` is the named-claim path's substitute for that observation: a
    drain reads every lease with the queue, a `claim <issue>` has read nothing,
    so it looks the lease up itself — but only once the cheap label guard has
    already let the task through, so a task held by `needs-decision` or
    `awaiting-merge` still costs exactly one read to refuse.

    The guard reads two labels, not three: `in-progress` is write-only (§3), so
    the lease below decides everything it used to. That collapses what was this
    function's hardest branch — a label that held only conditionally, probed
    mid-guard — into the plain rule the lock already stated."""
    ttl = lease_ttl_seconds(ttl)

    def _stealing(l):
        return l is not None and not l["live"] and l["worker"] != worker

    # 1. Guard — re-fetch labels; a held task is skipped with zero writes.
    label_names = issue_label_names(repo, issue)
    if label_names is None:
        diag(f"claim: gh-failure issue={issue} stage=guard")
        return EXIT_TRANSPORT
    for held in HELD_LABELS:
        if held in label_names and held not in allow_held:
            diag(f"claim: held issue={issue} label={held}")
            return EXIT_NOT_CLEAR
    if probe_lease and lease is None:
        # Labels say nothing about who is working — that is the lease's job, and
        # this path has not read one yet. It answers both questions at once: who
        # holds the task (the check below), and which generation the CAS must
        # create — the one above whatever is actually there, since creating a
        # LOWER one would "win" a ref nobody is holding by while the real holder
        # works on.
        lease = probe_lease_state(repo, issue, ttl)
        if lease is False:
            diag(f"claim: gh-failure issue={issue} stage=lease")
            return EXIT_TRANSPORT

    # A live lease that is not ours holds the task whatever the labels say — the
    # lock is the whole of "somebody is working on this" (§5), including in the
    # crash window where no label has landed yet, and including when the badge
    # says otherwise. This is a LOST claim, not an unclear one: somebody
    # owns the task. Under protocol/5 the same case surfaced by attempting the
    # CAS and reading its 422; the generation ladder makes that attempt worse
    # than useless — creating the next generation would SUCCEED and take a live
    # lease — so the read answers it instead, with the same verdict.
    if lease is not None and lease["live"] and lease["worker"] != worker:
        diag(f"claim: lost-cas issue={issue} — another worker holds the claim ref")
        return EXIT_LOST

    steal = lease if _stealing(lease) else None
    # The requeued labels still on the issue: dropped as part of the projection.
    stale_held = [h for h in HELD_LABELS if h in label_names and h in allow_held]

    # 2. CAS — an orphan claim commit, then the create that arbitrates: the
    #    generation above whatever was observed. It is the SAME conflict-failing
    #    write on the SAME ref name for a first claim, a steal and a renewal, so
    #    N thieves racing one expired lease — and the holder racing them with a
    #    renewal — produce exactly one winner and nothing to undo. Nothing is
    #    deleted to make room, so the task is never observably free mid-steal and
    #    no interleaving can hand two workers the same lease.
    from_gen = lease["gen"] if lease is not None else LEGACY_CLAIM_GEN
    outcome, gen, _sha = advance_lease(
        repo, issue, from_gen, {"type": "claim", "worker": worker})
    if outcome.startswith("fail"):
        diag(f"claim: gh-failure issue={issue} stage={outcome[len('fail-'):]}")
        return EXIT_TRANSPORT
    if outcome == "lost":
        # A 422 is a real loss only if the lease is ANOTHER worker's. A worker
        # re-claiming its own in-flight generation after a network failure (§5)
        # already owns the task, so fall through to re-project. An unreadable
        # owner counts as not ours.
        if claim_ref_owner(repo, issue) != worker:
            diag(f"claim: lost-cas issue={issue} — another worker holds the claim ref")
            return EXIT_LOST

    # The generations we climbed past are garbage now — the holder is the highest
    # one, so a delete that fails leaves a stray ref, never a second lease.
    if lease is not None and gen is not None:
        drop_generations(repo, issue, [g for g in lease["gens"] if g < gen])

    # 3. Projection. State file FIRST so lifecycle hooks can release the claim even
    #    if the writes below fail; then the in-progress label and claim comment. A
    #    failure here leaves the claim HELD by the lease — exit 20 says re-check.
    #    The label is the only thing that can be left behind, and nothing reads it
    #    (§3): the task stays correctly held, just without its badge, until the
    #    terminal transition writes the next one.
    write_claim_state(repo, issue, worker)
    if steal is not None and not post_comment(
            repo, issue, lease_expired_body(worker, steal)):
        diag(f"claim: gh-failure issue={issue} stage=comment (claim held)")
        return EXIT_TRANSPORT
    for stale in stale_held:
        if not swap_labels(repo, issue, remove=stale):
            diag(f"claim: gh-failure issue={issue} stage=label (claim held)")
            return EXIT_TRANSPORT
    if not swap_labels(repo, issue, add="in-progress"):
        diag(f"claim: gh-failure issue={issue} stage=label (claim held)")
        return EXIT_TRANSPORT
    body = compose_comment(
        worker, "Claimed this task — starting work now.",
        {"type": "claim", "worker": worker},
    )
    if not post_comment(repo, issue, body):
        diag(f"claim: gh-failure issue={issue} stage=comment (claim held)")
        return EXIT_TRANSPORT

    verb = "stole" if steal is not None else "claimed"
    diag(f"claim: {verb} issue={issue} worker={worker}")
    return EXIT_OK


def cmd_claim(args):
    refused = refuse_second_claim(args.worker, args.issue)
    if refused is not None:
        return refused
    # A named claim has read no queue, so it looks the lease up itself: an
    # expired one does not hold the task, it gets stolen (§5).
    return _claim_once(args.repo, args.issue, args.worker, probe_lease=True)


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
    iterates forward, never back.

    The loop itself lives in `acquire_next`; this is its line-oriented rendering
    (the briefing a human or a shell reads). `next-action` renders the same
    result as a JSON envelope."""
    rc, won = acquire_next(args.repo, args.project, args.worker)
    if rc == EXIT_OK:
        if args.json:
            print(json.dumps(won))
        else:
            diag(f"claim-next: claimed issue={won['issue']} worker={args.worker}")
            print(f"{won['issue']}\t{won['title']}")
            print()
            print(won["body"])
    return rc


def acquire_next(repo, project, worker, ttl=None):
    """The whole acquisition — one-task-at-a-time guard, project preflight, queue
    read, §6 reconcile, then guard + CAS down the candidate list — as DATA rather
    than as printed output: returns `(exit_code, won)` where `won` is
    `{"issue", "title", "body"}` on EXIT_OK and None on every other outcome.

    `claim-next` and `next-action` are both thin renderings of this, which is
    what keeps the deterministic claim loop from drifting between them: there is
    one implementation and two presentations. Diagnostics go through `diag`, so
    `next-action` can push them to stderr and keep stdout pure JSON."""
    refused = refuse_second_claim(worker)
    if refused is not None:
        return (refused, None)

    # Project preflight: before the queue is read and before any write,
    # because a worker scoped to a project label the repo does not carry is deaf,
    # not idle — it would report an honest-looking empty queue forever.
    ok, message = verify_project(repo, project)
    if ok is None:
        diag(message)
        return (EXIT_TRANSPORT, None)
    if not ok:
        diag(message)
        return (EXIT_UNKNOWN_PROJECT, None)

    ttl = lease_ttl_seconds(ttl)
    read = read_queue(repo, ttl=ttl)
    if read is None:
        diag("claim-next: gh-failure stage=list")
        return (EXIT_TRANSPORT, None)
    nodes, leases = read

    # Reconcile before classifying (PROTOCOL.md §6). A dead worker's lease
    # obstructs exactly one party — the next worker who wants to claim — and that
    # is this one, so the repair belongs here rather than in an hourly cron. The
    # lease state came with the queue read, so the whole reconcile costs NOTHING
    # extra, and an expired lease costs no write at all: it is simply not held,
    # and the claim below steals it. The pass is repo-wide (not project-scoped)
    # because a lock is repo-wide, exactly like the cron it replaces.
    plan = reconcile_plan(nodes, leases)
    if plan:
        if apply_reconcile(repo, plan, worker) is None:
            # Writes of ours may have half-landed — same state-unknown rule as a
            # failed claim: re-check before retrying, never push on.
            diag("claim-next: gh-failure stage=reconcile — state unknown, re-check")
            return (EXIT_TRANSPORT, None)
        project_reconcile(plan, nodes, leases)

    rows = classify_queue(repo, project, include_body=True, read=read)
    if rows is None:
        diag("claim-next: gh-failure stage=list")
        return (EXIT_TRANSPORT, None)

    by_number = {n["number"]: n for n in nodes}
    live = live_leases(leases)
    for number, title, _created, state, body in rows:  # priority-first, then FIFO
        if state != "startable":
            continue
        # Re-derive the requeue verdict from the same node the filter used — a
        # pure call, no request — so the guard accepts a task an operator reply
        # has already requeued instead of refusing what was just offered.
        allow_held = requeued_labels(by_number[number], live)
        # Hand the claim the lease this read already saw: it decides which
        # generation the CAS starts from, and whether this is a steal at all.
        rc = _claim_once(repo, number, worker, allow_held=allow_held,
                         lease=leases.get(number), ttl=ttl)
        if rc == EXIT_OK:
            return (EXIT_OK, {"issue": number, "title": title, "body": body})
        if rc == EXIT_TRANSPORT:
            # State is now ambiguous — do NOT move on to another candidate while
            # a write of ours may have half-landed. Re-check before any retry.
            diag(f"claim-next: gh-failure issue={number} — state unknown, re-check")
            return (EXIT_TRANSPORT, None)
        # EXIT_LOST (10) / EXIT_NOT_CLEAR (11): back off, try the next candidate.

    diag(f"claim-next: none project:{project}")
    return (EXIT_NONE, None)


# --- subcommand: next-action -------------------------------------------------
#
# The driver loop, as one call. Everything a worker otherwise has to REMEMBER
# between transitions — whether it already holds a task, whether that lease is
# still its own, when the lease must be renewed, which writes are legal next and
# with which arguments — is bookkeeping, and bookkeeping belongs in the program.
# What is left for the agent is the part only it can do: read the goal, write the
# code, write the question, write the result.
#
# This is a worker-side ergonomic (PROTOCOL.md §12), exactly like claim-next and
# status: no new wire semantics and no new write. The only writes it performs are
# the claim path's, reached through acquire_next.
#
# stdout is the JSON envelope and NOTHING else, so the whole computation runs
# with `diag` routed to stderr (see diagnostics_on_stderr).

# The action vocabulary, single-sourced here: `kraken.py contract next-actions`
# prints it and the skill lint checks the SKILL.md documents every one, the same
# execute-don't-diff rule the marker types follow.
NEXT_ACTIONS = ("execute", "idle", "abandon", "blocked", "stop", "retry")

# action -> the exit code that carries the same verdict to a shell. They reuse
# the established contract (§12) rather than inventing a second numbering: a
# caller that already branches on claim-next's codes needs to learn nothing.
NEXT_ACTION_EXIT = {
    "execute": EXIT_OK,
    "idle": EXIT_NONE,
    "abandon": EXIT_LOST,
    "blocked": EXIT_NOT_CLEAR,
    "stop": EXIT_UNKNOWN_PROJECT,
    "retry": EXIT_TRANSPORT,
}


def format_iso(epoch):
    """Epoch seconds as an ISO-8601 UTC timestamp (…Z) — `parse_iso`'s inverse,
    so a timestamp this program emits is one it can read back."""
    return datetime.datetime.fromtimestamp(
        epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def then_commands(repo, issue, worker, script=None):
    """The exact command lines this worker may run next, fully interpolated —
    the script's own absolute path, the repo, the issue and the worker name
    already filled in, with only the parts that are the agent's to write left as
    placeholders.

    This is the point of the envelope: assembling an argv is bookkeeping, and an
    agent that assembles one gets the path wrong, or the argument order, or the
    worker name. The path is quoted because a plugin folder may contain spaces."""
    script = script or os.path.abspath(__file__)
    base = f'python3 "{script}"'
    return {
        "renew": f"{base} heartbeat {repo} {issue} {worker} '<one line of progress>'",
        "note": f"{base} note {repo} {issue} {worker} <body-file>",
        "escalate": f"{base} escalate {repo} {issue} {worker} <question-file>",
        "deliver": f"{base} deliver {repo} {issue} {worker} <result-file> <pr-url>",
        "release": f"{base} release {repo} {issue} {worker} '<reason>'",
    }


def lease_block(epoch, now, ttl, generation=None, source="claim-ref"):
    """The renewal contract as numbers instead of as an instruction to remember:
    when this lease dies, how long is left, and how often to renew.

    `source` says where the clock came from. "claim-ref" is the authoritative
    one — the ref's server-stamped commit date, the same anchor every reader
    applies the TTL to (§5.1). "estimated" is the freshly-won claim, whose commit
    was created moments ago and is timed off the local clock rather than paying a
    re-read for it: renewal is TTL/3, so a local clock has three renewals of
    slack before drift could matter, and the authoritative clock still decides
    every actual expiry."""
    expires = epoch + ttl
    return {
        "generation": generation,
        "renew_every_seconds": lease_renew_seconds(ttl),
        "expires_at": format_iso(expires),
        "seconds_remaining": int(expires - now),
        "renew_now": expires <= now,
        "source": source,
    }


def task_brief(title, body):
    """The task as the agent needs it: the issue-form sections split out, plus
    the raw body for a hand-written issue that carries no headings. An empty or
    `_No response_` section reads as "" rather than as the placeholder text."""
    def section(name):
        content = section_body(body, name)
        return "" if is_empty_section(content) else content.strip()

    return {
        "title": title,
        "goal": section("Goal"),
        "acceptance": section("Acceptance"),
        "notes": section("Notes"),
        "body": body,
    }


def next_action_envelope(action, repo, worker, *, issue=None, resumed=None,
                         brief=None, lease=None, reason=None, detail=None,
                         holding=None, script=None):
    """The one JSON shape next-action emits. `action` is the verdict, `reason` a
    stable machine slug for it, and `detail` the human sentence — so a consumer
    branches on the slug and a human reads the sentence, never the other way
    round.

    `holding` is `{"repo", "issue"}` naming a claim this worker must resolve, and
    it exists because a `blocked` claim may live in a **different repo** than the
    one being drained: pairing the top-level `repo` with that claim's issue
    number would name a task that does not exist. So `blocked` reports the claim
    under `holding` and never as a bare top-level `issue`.

    `then` is attached wherever a write is actually legal: to `execute` for the
    task in hand, and to `blocked` for the claim that has to be resolved first —
    built against **that** claim's repo and issue. Emitting `blocked` without it
    would tell a worker to run a transition it has no way to construct."""
    env = {"action": action, "repo": repo, "worker": worker}
    if issue is not None:
        env["issue"] = int(issue)
    if resumed is not None:
        env["resumed"] = resumed
    if reason:
        env["reason"] = reason
    if detail:
        env["detail"] = detail
    if holding is not None:
        env["holding"] = {"repo": holding["repo"], "issue": int(holding["issue"])}
    if brief is not None:
        env["brief"] = brief
    if lease is not None:
        env["lease"] = lease
    if action == "execute" and issue is not None:
        env["then"] = then_commands(repo, issue, worker, script=script)
    elif action == "blocked" and holding is not None:
        env["then"] = then_commands(holding["repo"], int(holding["issue"]),
                                    worker, script=script)
    return env


def issue_is_finished(issue_obj):
    """Whether an issue has left the state where "keep executing" makes sense:
    closed, or carrying a terminal held label. A worker whose state file still
    names such a task already finished with it — the transition landed and only
    the local scratch file lagged."""
    if str((issue_obj or {}).get("state", "")).upper() == "CLOSED":
        return True
    names = {lbl.get("name", "") for lbl in (issue_obj or {}).get("labels", [])}
    return bool(names & {"needs-decision", "awaiting-merge"})


def resume_verdict(record, repo, worker, ref_state, head, issue_obj, now, ttl):
    """Decide what a recorded open claim is worth, as a pure function of what was
    observed. Returns `(verdict, detail)` where verdict is one of:

      - `"blocked"`  — the record names a claim in a DIFFERENT repo. Decided
        before any read, so the caller passes None for `ref_state`/`head`/
        `issue_obj`; one task at a time is repo-independent (§5).
      - `"retry"`    — a read did not land. Ambiguous is never a decision:
        write nothing, change nothing, re-check.
      - `"resolved"` — this worker no longer holds the task AND the task has
        already left the queue (closed, or `needs-decision`/`awaiting-merge`).
        The transition landed and the state file lagged, so the drain simply
        continues to the next task.
      - `"abandon"`  — this worker no longer holds the lease on a task that is
        still open and unfinished: it was stolen, or the ref is gone. Write
        NOTHING (§5.3) and say so.
      - `"execute"`  — the lease is still ours. Resume.

    Ownership is checked before the issue's own state, because the lease is the
    lock and the labels are projection. A lease that is ours but PAST its TTL
    still resumes: §5.3 is explicit that an expired-but-unstolen lease may
    complete its transition — the caller is told to renew first."""
    if record["repo"] and record["repo"] != repo:
        return ("blocked", {"reason": "claim-elsewhere",
                            "detail": f"this worker holds {record['repo']}"
                                      f"#{record['issue']}; resolve it (deliver / "
                                      f"escalate / release) before draining {repo}"})
    if ref_state == "error":
        return ("retry", {"reason": "lease-unreadable",
                          "detail": "the claim ref did not read — re-check "
                                    "before writing anything"})
    if issue_obj is None:
        return ("retry", {"reason": "issue-unreadable",
                          "detail": "the task issue did not read — re-check "
                                    "before writing anything"})

    ours = ref_state == "ok" and (head or {}).get("worker") == worker
    if not ours:
        if issue_is_finished(issue_obj):
            # Whether this worker's own terminal transition deleted the ref or
            # the reconciler reclaimed it is not answerable from here — and the
            # right move is the same either way: the task is not ours and not in
            # the queue, so keep draining rather than report a loss.
            return ("resolved", {"reason": "already-resolved"})
        holder = (head or {}).get("worker")
        if ref_state == "absent":
            return ("abandon", {"reason": "lease-gone",
                                "detail": "the claim ref is gone — re-claim the "
                                          "task before writing to it"})
        return ("abandon", {"reason": "lease-stolen",
                            "detail": f"the lease is held by "
                                      f"{holder or 'another worker'} now — the "
                                      f"branch and PR you pushed stay, and "
                                      f"whoever holds the task inherits them"})
    if issue_is_finished(issue_obj):
        # Our lease, but the task ended under us (an operator closed or answered
        # it). Nothing to resume; the lease frees itself within one TTL.
        return ("resolved", {"reason": "task-finished"})

    return ("execute", {"generation": (head or {}).get("gen"),
                        "epoch": (head or {}).get("epoch")})


def _resume(record, repo, worker, ttl, now, script):
    """Fetch what `resume_verdict` needs and turn its verdict into an envelope.
    Returns `(exit_code, envelope)`, or `(None, None)` when the recorded claim is
    resolved and the caller should go acquire a new task."""
    issue = record["issue"]
    issue_obj = None

    if record["repo"] and record["repo"] != repo:
        verdict, detail = resume_verdict(record, repo, worker, None, None,
                                         None, now, ttl)
    else:
        ref_state, head = claim_ref_head(repo, issue)
        # One GET serves both the finished-task check and the brief.
        issue_obj = None if ref_state == "error" else issue_detail(repo, issue)
        verdict, detail = resume_verdict(record, repo, worker, ref_state, head,
                                         issue_obj, now, ttl)

    if verdict == "resolved":
        # The claim is over. Drop the stale scratch file so the one-task-at-a-time
        # guard stops refusing on it, and let the caller drain on. This is the
        # self-heal that used to need an operator running `release` by hand.
        clear_claim_state(worker)
        diag(f"next-action: claim resolved issue={issue} ({detail['reason']}) "
             "— continuing the drain")
        return (None, None)

    if verdict == "abandon":
        # Provably not ours: clearing local state is safe and is what unjams this
        # worker. An AMBIGUOUS read never reaches here (it is "retry"), so no
        # transport fault can make us drop a claim we might still hold.
        clear_claim_state(worker)

    action = verdict  # the remaining verdicts are the action names themselves
    if action != "execute":
        diag(f"next-action: {action} issue={issue} ({detail['reason']})")
        # `blocked` names the claim under `holding` (it may be in another repo,
        # so a bare top-level issue would read against the wrong one) and carries
        # the commands that resolve it. The other verdicts are about the task in
        # this repo, and none of them makes a write legal.
        if action == "blocked":
            return (NEXT_ACTION_EXIT[action], next_action_envelope(
                action, repo, worker, reason=detail["reason"],
                detail=detail.get("detail"), script=script,
                holding={"repo": record["repo"] or repo, "issue": issue}))
        return (NEXT_ACTION_EXIT[action], next_action_envelope(
            action, repo, worker, issue=issue, reason=detail["reason"],
            detail=detail.get("detail"), script=script))

    epoch = detail["epoch"]
    # An unreadable clock is an expired lease (§5.1) — fail open, toward renewing
    # immediately, never toward assuming time is left.
    lease = lease_block(epoch if epoch is not None else now - ttl, now, ttl,
                        generation=detail["generation"])
    diag(f"next-action: resumed issue={issue} worker={worker}")
    return (EXIT_OK, next_action_envelope(
        "execute", repo, worker, issue=issue, resumed=True,
        brief=task_brief(issue_obj.get("title") or "", issue_obj.get("body") or ""),
        lease=lease, script=script))


def next_action(repo, project, worker, ttl=None, now=None, script=None):
    """The driver loop's single call: resume the task this worker already holds,
    or acquire the next one. Returns `(exit_code, envelope)`."""
    ttl = lease_ttl_seconds(ttl)
    now = time.time() if now is None else now

    record = open_claim_record(worker)
    if record is not None:
        rc, env = _resume(record, repo, worker, ttl, now, script)
        if env is not None:
            return (rc, env)
        # Resolved: fall through and take the next task.

    rc, won = acquire_next(repo, project, worker, ttl=ttl)
    if rc == EXIT_OK:
        return (EXIT_OK, next_action_envelope(
            "execute", repo, worker, issue=won["issue"], resumed=False,
            brief=task_brief(won["title"], won["body"]),
            lease=lease_block(now, now, ttl, source="estimated"),
            script=script))
    if rc == EXIT_NONE:
        return (EXIT_NONE, next_action_envelope(
            "idle", repo, worker, reason="queue-empty",
            detail=f"nothing startable in project:{project}",
            script=script))
    if rc == EXIT_UNKNOWN_PROJECT:
        return (EXIT_UNKNOWN_PROJECT, next_action_envelope(
            "stop", repo, worker, reason="unknown-project",
            detail=f"the repo carries no project:{project} label — this worker "
                   "would filter every task out; fix the label, never drain "
                   "unscoped",
            script=script))
    if rc == EXIT_NOT_CLEAR:
        # A claim appeared between the resume check and the acquisition. Re-read
        # the record so this envelope, like the other `blocked`, names the claim
        # to resolve and carries the commands that resolve it.
        late = open_claim_record(worker)
        return (EXIT_NOT_CLEAR, next_action_envelope(
            "blocked", repo, worker, reason="claim-open",
            detail="this worker already holds an open claim — resolve it "
                   "(deliver / escalate / release) first",
            holding=({"repo": late["repo"] or repo, "issue": late["issue"]}
                     if late else None),
            script=script))
    return (EXIT_TRANSPORT, next_action_envelope(
        "retry", repo, worker, reason="transport",
        detail="the queue read or a claim write did not land — re-check the "
               "task's real state before retrying",
        script=script))


def render_next_action(env):
    """The --text rendering: the same verdict as one line a human can scan,
    plus what the agent owes next. The JSON stays the machine contract."""
    action = env["action"]
    head = f"next-action: {action}"
    if "issue" in env:
        head += f" issue={env['issue']}"
    head += f" repo={env['repo']} worker={env['worker']}"
    if env.get("holding"):
        head += f" holding={env['holding']['repo']}#{env['holding']['issue']}"
    if env.get("resumed") is not None:
        head += f" resumed={str(env['resumed']).lower()}"
    print(head)
    if env.get("detail"):
        print(f"  {env['detail']}")
    brief = env.get("brief")
    if brief:
        print(f"  title: {brief['title']}")
        for field in ("goal", "acceptance"):
            if brief[field]:
                first = brief[field].split("\n")[0]
                print(f"  {field}: {first}")
    lease = env.get("lease")
    if lease:
        print(f"  lease: renew every {lease['renew_every_seconds']}s; "
              f"expires {lease['expires_at']} "
              f"({lease['seconds_remaining']}s left"
              f"{', RENEW NOW' if lease['renew_now'] else ''})")
    for name, command in (env.get("then") or {}).items():
        print(f"  {name}: {command}")


def cmd_next_action(args):
    with diagnostics_on_stderr():
        rc, env = next_action(args.repo, args.project, args.worker)
    if args.text:
        render_next_action(env)
    else:
        print(json.dumps(env, indent=2))
    return rc


# --- subcommand: heartbeat ---------------------------------------------------

def cmd_heartbeat(args):
    """Renew the lease: climb one generation with a fresh commit whose
    server-stamped date restarts the TTL and whose marker carries the progress
    text. No timeline comment — `status` surfaces the age and message from the
    ref. A worker holding a lease renews every TTL/3 (`contract lease-renew`).

    The renewal is a CAS, not an in-place update, and that is the point: a thief
    takes the task by creating the generation above the holder's, so a renewal
    and a steal compete to create the SAME ref and the server settles it. There
    is no interleaving in which a live worker is stolen from *and* believes it
    still holds the lease — the loser is told, immediately, with exit 10."""
    repo, issue, worker, message = args.repo, args.issue, args.worker, args.message
    lost, head = hold_lease("heartbeat", repo, issue, worker)
    if lost is not None:
        return lost
    outcome, gen, _sha = advance_lease(
        repo, issue, head["gen"],
        {"type": "heartbeat", "worker": worker, "msg": message},
    )
    if outcome.startswith("fail"):
        print(f"heartbeat: gh-failure issue={issue} stage={outcome[len('fail-'):]}")
        return EXIT_TRANSPORT
    if outcome == "lost":
        print(f"heartbeat: lost-lease issue={issue} — another worker took the "
              "lease while this one was silent")
        return EXIT_LOST
    drop_generations(repo, issue, [g for g in head["gens"] if g < gen])
    print(f"heartbeat: renewed issue={issue} worker={worker}")
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

    # The lease first (§5): a question posted onto a task another worker is now
    # executing puts a decision in front of the operator that nobody is waiting on.
    lost, head = hold_lease("escalate", repo, issue, worker)
    if lost is not None:
        return lost

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
    if not drop_generations(repo, issue, head["gens"]):
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

    # The write-after-expiry check (§5), before a single write. This is the case
    # the short TTL is built around: a worker that went silent long enough to be
    # stolen from may still finish and come back to deliver, and its result would
    # land on a task another worker is now executing — two deliveries, one task,
    # and a review queue that lies. The work is not lost: the branch and PR
    # exist, and re-claiming the task delivers them honestly.
    lost, head = hold_lease("deliver", repo, issue, worker)
    if lost is not None:
        return lost

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
    if not drop_generations(repo, issue, head["gens"]):
        print(f"deliver: gh-failure issue={issue} stage=ref")
        return EXIT_TRANSPORT

    clear_claim_state(worker)
    suffix = f" pr={pr_url}" if pr_url else ""
    print(f"deliver: delivered issue={issue} worker={worker}{suffix}")
    return EXIT_OK


# --- subcommand: release -----------------------------------------------------

def cmd_release(args):
    repo, issue, worker, reason = args.repo, args.issue, args.worker, args.reason

    # Releasing a lease that is no longer ours would delete the NEW holder's
    # lease and strip the label out from under a worker mid-task — the one way a
    # well-meaning cleanup can corrupt someone else's claim. So the check is not
    # optional here, and the local state file is cleared either way: the caller
    # (a lifecycle hook, the ambush loop) must not be left retrying forever a
    # release that is no longer its business.
    lost, head = hold_lease("release", repo, issue, worker)
    if lost is not None:
        if lost == EXIT_LOST:
            clear_claim_state(worker)
        return lost

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
    if not drop_generations(repo, issue, head["gens"]):
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
    worker-authored write the skill otherwise had you hand-assemble, disclaimer
    and all. next-action offers it as `then.note`."""
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


def compute_status(repo, project, nodes, now, *, leases, commit_meta,
                   comment_reader, pr_merged, project_lister):
    """Pure-ish status computation, transport injected so it is unit-testable:
    given the queue nodes (from fetch_open_tasks), the lease state of every claim
    ref + their commit meta, and reader callbacks, build the
    review/decision/in-flight/projects report. Returns the report dict, or None
    on any injected-transport failure (propagated as exit 20)."""
    # Queue hygiene reads the UNFILTERED walk on purpose: the most valuable case
    # it reports — a task carrying no project label at all — is precisely the one
    # a project scope would hide, since the task belongs to no project.
    hygiene = queue_hygiene(nodes, project)
    live = live_leases(leases)

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
                  if l not in requeued_labels(node, live)]

        if "awaiting-merge" in labels:
            records = comment_reader(repo, number)
            if records is None:
                return None
            pr_url, pr_source = parse_pr_url(records)
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
                           "pr_source": pr_source, "orphan": orphan,
                           "merge_state_unknown": merge_state_unknown})
        elif "needs-decision" in labels:
            decision.append({"number": number, "title": title})
        elif number in leases:
            # In flight is the LEASE, never the label (§3): the label is written
            # for the human reading the issue list and nothing repairs it, so a
            # console that read it would strand a "worker unknown" row on every
            # task whose release crashed — forever. Keying on the lease also
            # covers the opposite window, a claim whose label has not landed yet.
            lease = leases[number]
            worker, msg, anchor = claim_meta_of(lease["sha"], commit_meta)
            age = lease["age"]
            # An expired lease is not a claim any more: the next drain to read
            # the queue steals it, no repair pass involved. The console still has
            # to say so out loud — a lease due to expire in a minute looks
            # exactly like one being renewed, and only the operator can tell
            # which of their workers is actually alive.
            in_flight.append({"number": number, "title": title, "worker": worker,
                              "heartbeat_anchor": anchor,
                              "heartbeat_age_seconds": age,
                              "heartbeat_msg": msg,
                              "stale": not lease["live"]})

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
        # A link the delivery never recorded is a guess off the thread's prose —
        # say so, so the operator knows which links the worker stands behind.
        if item["pr_url"] and item.get("pr_source") == "legacy-free-text":
            link += "  (legacy: read from free text, no delivered marker)"
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
        # A silent lease reads exactly like a busy one, and nothing repairs it on
        # a schedule now, so say which one it is.
        flag = "  ⚠️  lease expired — the next drain steals it" if item.get("stale") else ""
        lines.append(f"     #{item['number']}  {item['title']}  · worker {worker} "
                     f"· lease renewed {age} ago{note}{flag}")
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
    commit_meta = resolve_commit_meta(repo, holder_shas(claim_refs))
    if commit_meta is None:
        print("status: gh-failure stage=commits", file=sys.stderr)
        return EXIT_TRANSPORT

    now = time.time()
    leases = lease_state(claim_refs, commit_meta, now, lease_ttl_seconds())
    # The console reads the queue the way a worker does, so it needs the same
    # windows the requeue derivation needs — and only those. Hygiene reads bodies
    # and the review queue reads its own paginated thread, neither of which this
    # touches.
    if hydrate_comment_windows(repo, nodes, leases) is None:
        print("status: gh-failure stage=comments", file=sys.stderr)
        return EXIT_TRANSPORT

    report = compute_status(
        repo, project, nodes, now,
        leases=leases,
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

def bundled_asset(name):
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


def gh_put_content(repo, path, data, message):
    """CREATE `path` on the repo with `data` via the contents API. Callers reach
    here only when the file was reported absent — an overwrite would need the
    current blob sha, and init never overwrites."""
    body = {"message": message, "content": base64.b64encode(data).decode("ascii")}
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
    labels. Create-only: an asset that already exists is left exactly as it is,
    hand edits included — nothing in the coordination repo is executed any more
    (protocol/5), so a file that differs from the bundled copy is the operator's
    business, not a hazard to police. Re-running init on an older coordination
    repo is also its MIGRATION: assets this protocol revision retired are deleted
    (OBSOLETE_ASSETS). Idempotent; touches no issues. Exit 0 on success, 20 on
    any gh/transport failure."""
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

    # 2. Install the bundled assets, create-only — an existing file is reported
    #    and left alone.
    for name, dest, message in INIT_ASSETS:
        try:
            bundled = bundled_asset(name)
        except OSError:
            print(f"init: missing bundled asset {name}", file=sys.stderr)
            return EXIT_USAGE
        current, _sha = gh_get_content_meta(repo, dest)
        if current is None:
            if not gh_put_content(repo, dest, bundled, message):
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
    lines.append(
        f"init: done repo={report['repo']} repo_status={report['repo_status']} "
        f"assets_created={count('created')} assets_present={count('present')} "
        f"assets_removed={count('removed')} labels={len(report['labels'])}"
    )
    return "\n".join(lines)



# --- coordination-repo subcommands -------------------------------------------
# The logic-bearing coordination passes (reconcile, validate-task) live here
# rather than re-implementing the protocol parse in jq/grep/awk — ONE parser,
# sharing the marker decoder, disclaimer, and label vocabulary (and unit tests)
# with the worker side.

# --- the reconciler (PROTOCOL.md §6) -----------------------------------------
# The claim ref is the lease and its commit date the lease timestamp. Under
# protocol/5 the READER reconciles it, not a cron in the coordination repo: a
# dead worker's lease obstructs exactly one party — the next worker who wants to
# claim — and there is no other observer of it, so the reconcile rides the claim
# path (cmd_claim_next) on the queue read it already paid for. `reap` keeps the
# same pass as an operator-side escape hatch; both go through the pure planner
# below, so the two entry points can never drift.
#
# This pass has only ever got SMALLER. Protocol/6 dropped the expired lease from
# it: an expired lease is not a repair, it is simply not held, and the claim
# steals it (§5) at no write. Protocol/7 dropped the other half — the two rules
# that existed to make the `in-progress` label and the leases agree (heal a
# missing label, strip an orphan one). Nothing reads that label now (§3), so
# there is nothing to agree WITH and no disagreement to repair.
#
# What is left is exactly the state the leases cannot fix on their own: a lock
# over a task that has moved on, and a task that keeps expiring.

# Who a stand-alone `reap` attributes its comments to when the operator names no
# worker. A drain passes its own worker name instead: under protocol/5 the
# reconciler's comments are posted by a worker's token, so they carry the §4
# attribution disclaimer like every other worker comment.
RECONCILER_WORKER = "reconciler"


def stale_claim_body(worker, reason):
    """The reconciler's reclaim comment: the attribution disclaimer, human prose,
    and the stale-claim marker (audit trail). The disclaimer is required because
    a worker posts this now — under protocol/4 it was the coordination repo's own
    Actions bot, which is why it used to carry none."""
    prose = (
        f"Nobody is finishing this task ({reason}). Stealing the lease again "
        "would just burn another worker, so it needs a human call. To requeue, "
        "reply on this thread — or remove the needs-decision label by hand."
    )
    return compose_comment(
        worker, prose, {"type": "stale-claim", "reason": reason}
    )


def expiry_count(node):
    """How many times this task's lease has already expired and been stolen —
    counted from the `lease-expired` markers each steal leaves (§5). Only reached
    for a task whose lease EXPIRED, which is exactly what makes the task one the
    hydration pass filled (see comment_hungry), so the count reads a window that
    is already in hand; a window that has scrolled past the oldest steals
    undercounts, which errs toward giving the task another worker rather than
    escalating early."""
    total = 0
    for rec in comment_nodes_of(node):
        for line in (rec.get("body") or "").split("\n"):
            payload = parse_marker(line)
            if payload and payload.get("type") == "lease-expired":
                total += 1
    return total


def reconcile_plan(nodes, leases, max_expiries=LEASE_EXPIRY_ESCALATE):
    """The reconciler's decision as a PURE function of one queue read — no
    network, so every rule is unit-testable in isolation (PROTOCOL.md §6). The
    expiry itself is already decided (`lease_state`); these are the repairs the
    expiry does NOT make on its own:

      1. **orphan lock** — a ref whose issue is not an open task any more, or is
         already labeled needs-decision/awaiting-merge (a terminal transition
         whose ref delete was lost): delete the ref, touch nothing else;
      2. **reclaim** — a task whose lease has expired `max_expiries` times
         already: move it to needs-decision with a stale-claim comment, then
         delete the ref. This is the ONLY thing an expiry escalates. A task that
         kills whatever worker touches it — a poisoned environment, a step that
         always hangs — would otherwise be stolen, dropped, stolen again for as
         long as the queue has workers, each round burning a drain and telling
         nobody. Anything below the threshold is not the reconciler's business:
         the lease is simply not held, and the claim path steals it.

    Both rules are about the LEASE. Under protocol/6 there were two more, both
    about the in-progress LABEL: restore one a crashed claim never wrote, strip
    one a crashed release never removed. Protocol/7 made that label write-only
    (§3), so a wrong badge misleads nobody and misroutes nothing — it is not a
    repair the reader owes anyone, and the next transition on the task overwrites
    it anyway.

    `nodes` is the open-kraken-task walk (repo-wide, before any project filter),
    so rule 1 needs no per-ref issue read: a ref on an issue absent from that
    walk is a ref on an issue that is closed, or no longer a task at all —
    either way it holds a lock over nothing. Nothing on the issue timeline
    anchors liveness, so an operator poking a dead worker's thread shortens
    time-to-triage rather than extending the lease.

    Returns a list of `{"rule", "issue", "reason"}` actions ordered by rule then
    issue number; an empty list means every lease is one a worker is entitled to
    hold (the overwhelmingly common case, and it costs zero writes)."""
    open_labels = {n["number"]: set(label_names_of(n)) for n in nodes}
    by_number = {n["number"]: n for n in nodes}
    plan = []

    # Every rule is keyed on a lease, so the walk is over the refs — a task with
    # no ref has nothing for this pass to repair, whatever labels it wears.
    for num in sorted(leases):
        labels = open_labels.get(num)
        if (labels is None or "needs-decision" in labels
                or "awaiting-merge" in labels):
            plan.append({"rule": "orphan-lock", "issue": num,
                         "reason": "the task already left the claim",
                         "gens": leases[num]["gens"]})
            continue

        if not leases[num]["live"]:
            expiries = expiry_count(by_number[num])
            if expiries >= max_expiries:
                plan.append({"rule": "reclaim", "issue": num,
                             # The escalation writes needs-decision, and clears
                             # the in-progress badge if the task still wears one:
                             # write-only does not mean write-and-forget.
                             "reason": f"the lease expired {expiries} times and "
                                       "no worker has finished the task",
                             "held": "in-progress" in labels,
                             "gens": leases[num]["gens"]})
            # Below the threshold: not held, not repaired — stolen by the claim.

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
    counts = {"orphan-lock": 0, "reclaim": 0}
    for action in plan:
        rule, num = action["rule"], action["issue"]

        if rule == "orphan-lock":
            if not drop_generations(repo, num, action["gens"]):
                return _reconcile_failure("ref", num)
            diag(f"reap: orphan-lock issue={num} — claim ref deleted")

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
            if not drop_generations(repo, num, action["gens"]):
                return _reconcile_failure("ref", num)
            diag(f"reap: reclaimed issue={num} ({action['reason']})")

        counts[rule] += 1
    return counts


def project_reconcile(plan, nodes, leases):
    """Fold an APPLIED plan back into the in-memory queue read, in place, so the
    drain that just reconciled classifies the reconciled state without paying for
    a second fetch. Mirrors exactly what apply_reconcile wrote — nothing more:
    the leases it deleted, and the one label swap a reclaim performs."""
    by_number = {n["number"]: n for n in nodes}
    for action in plan:
        rule, num = action["rule"], action["issue"]
        if rule in ("orphan-lock", "reclaim"):
            leases.pop(num, None)
        node = by_number.get(num)
        if node is None:
            continue
        if rule == "reclaim":
            labels = (set(label_names_of(node)) - {"in-progress"}) | {"needs-decision"}
            node["labels"] = {"nodes": [{"name": n} for n in sorted(labels)]}


def cmd_reap(args):
    """Run the reconcile pass stand-alone — the operator-side escape hatch for
    the reconcile a drain performs on its own (PROTOCOL.md §6). One queue read
    (leases included), then the plan. Exit 0 on success, 20 on any gh/transport
    failure.

    Note what it will NOT do. It does not free an expired lease (protocol/6):
    expiry is applied by whoever READS the queue, so an expired lease is already
    unheld — running `reap` to "unstick" one is a no-op, and the drain that
    claims next steals it. And it does not touch the in-progress label
    (protocol/7): the label is write-only (§3), so a task wearing a stale one is
    already startable and there is nothing to repair."""
    repo = args.repo
    read = read_queue(repo, ttl=args.ttl)
    if read is None:
        print("reap: gh-failure stage=list", file=sys.stderr)
        return EXIT_TRANSPORT
    nodes, leases = read

    plan = reconcile_plan(nodes, leases)
    counts = apply_reconcile(repo, plan, args.worker)
    if counts is None:
        return EXIT_TRANSPORT

    print(
        f"reap: done leases={len(leases)} reclaimed={counts['reclaim']} "
        f"orphan_locks={counts['orphan-lock']}"
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

    # A closed task must not leave its lock behind: a crashed worker's lease
    # could linger past the close, and so could a generation a steal failed to
    # collect — drop every one of them. Idempotent: an already-absent ref counts
    # as deleted, and a task with no lease reads as an empty ladder.
    ok, refs = claim_refs_of(repo, issue)
    if not ok or not drop_generations(repo, issue, [g for g, _s in refs]):
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


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
