#!/usr/bin/env python3
"""In-process HTTP stub of GitHub's REST + GraphQL surface for the kraken
conformance suite (tests/conformance/harness.py starts one per test).

Phase-2 transport (issue #53) makes kraken.py talk pure HTTP over stdlib
urllib — no per-call `gh` subprocess — so the conformance seam is no longer a
`gh` binary on PATH but the API base itself: the harness points `GITHUB_API_URL`
at this server. It models exactly the shapes kraken.py sends, over the SAME
on-disk state layout the old `gh` CLI stub used, so every `mk_*` seeding helper
is unchanged. Being a real HTTP endpoint, it is what a third-party
implementation points its own transition executables at to validate conformance
(PROTOCOL.md §12).

State layout (under the server's state dir):
  issues/<n>/title       one line
  issues/<n>/createdAt   ISO timestamp
  issues/<n>/state       "open" | "closed"
  issues/<n>/labels      one label per line
  issues/<n>/body        issue body text (optional; absent = empty body)
  issues/<n>/blocked_by  one blocker issue number per line (optional)
  issues/<n>/comments/NNNN.md   bodies, NNNN = server order
  issues/<n>/comments/NNNN.at   optional ISO timestamp for that comment
  refs/<n>               protocol/5 claim ref refs/kraken/claims/<n> (generation
                         0); content = target sha
  refs/<n>-<gen>         claim ref refs/kraken/claims/<n>/<gen>; content = sha.
                         The holder of a task is its HIGHEST generation, and the
                         only way to take a lease is to CREATE the next one, so
                         the create below is the CAS for claims, steals and
                         renewals alike.
  objects/<sha>.json     git commit object: {"message", "committedDate"}
  pr/NNNN.json           a pull request: {"state", "mergedAt"}
  labels                 repo label registry, one per line
  labels-meta/<name>     "color=..\ndescription=.." for an upserted label
  repo/nameWithOwner     present iff the coordination repo "exists"
  repo/create.json       recorded on a repo-create POST ({"name","private"})
  contents/<path>        a file installed via the contents API
  log                    every request, one "METHOD path" line

Knobs (set by the harness on `.knobs` before a run — this server runs in the
test process, so they are shared attributes, not env vars):
  knobs.fail     an ERE; any request whose "METHOD path [graphql-query]" matches
                 answers HTTP 500 (the transport-fault injection, exit 20)
  knobs.barrier  N: the claim guard read (GET /repos/.../issues/<n>) blocks until
                 N callers arrive, then every caller reads one pre-race label
                 snapshot — the deterministic worst-case interleaving the claim
                 race test needs.
"""
import base64
import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _iso_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _blob_sha(data):
    """The git blob object sha of `data` — what GitHub's contents API returns as
    a file's `sha` and requires back to update it. Deterministic on content."""
    header = ("blob %d\0" % len(data)).encode("ascii")
    return hashlib.sha1(header + data).hexdigest()


class Knobs:
    """Per-test injection knobs, mutated by the harness between runs."""

    def __init__(self):
        self.lock = threading.RLock()   # serializes state mutations (the CAS)
        self.fail = None                # injected-failure ERE, or None
        self._barrier_n = 0
        self._barrier = None
        self._snapshot = None
        self._snap_lock = threading.Lock()

    def set_fail(self, regex):
        self.fail = regex

    def set_barrier(self, n):
        n = int(n or 0)
        self._barrier_n = n
        self._barrier = threading.Barrier(n) if n > 1 else None
        self._snapshot = None

    def set_cas_barrier(self, n):
        """Hold every ref CREATE until `n` of them have arrived. The guard
        barrier above synchronizes claimers at the label read, which only the
        claim path performs — this one synchronizes at the CAS itself, so a
        RENEWAL (which reads no labels) can be raced against a steal."""
        n = int(n or 0)
        self._cas_barrier = threading.Barrier(n) if n > 1 else None

    def cas_wait(self):
        if getattr(self, "_cas_barrier", None) is None:
            return
        try:
            self._cas_barrier.wait(timeout=30)
        except threading.BrokenBarrierError:
            pass

    def guard_wait(self, read_labels):
        """Hold every claim-guard caller until `barrier` of them arrive, then
        hand each the SAME label snapshot (captured once, before any writer could
        have advanced past its own guard). Without the snapshot a racer that
        arrives a hair later would read the winner's freshly-added in-progress
        label and mis-report exit 11 instead of the CAS loss (10) under test."""
        if self._barrier is None:
            return read_labels()
        try:
            self._barrier.wait(timeout=30)
        except threading.BrokenBarrierError:
            pass
        with self._snap_lock:
            if self._snapshot is None:
                self._snapshot = read_labels()
            return list(self._snapshot)


class StubState:
    """Reads and writes the on-disk state layout — the model behind the HTTP
    surface. All mutating calls run under the shared knobs lock."""

    def __init__(self, root, knobs):
        self.root = root
        self.knobs = knobs

    # --- low-level fs ---
    def path(self, *parts):
        return os.path.join(self.root, *parts)

    def _read(self, path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    def _write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def _lines(self, content):
        parts = content.split("\n")
        if parts and parts[-1] == "":
            parts.pop()
        return parts

    def label_names(self, content):
        return [x for x in content.rstrip("\n").split("\n") if x != ""]

    def issue_dir(self, n):
        return self.path("issues", str(n))

    def issue_dirs(self):
        idir = self.path("issues")
        if not os.path.isdir(idir):
            return []
        return [self.path("issues", e) for e in sorted(os.listdir(idir))
                if os.path.isdir(self.path("issues", e))]

    def is_open(self, d):
        return self._read(os.path.join(d, "state")).rstrip("\n") == "open"

    def issue_labels(self, n):
        p = os.path.join(self.issue_dir(n), "labels")
        return self.label_names(self._read(p)) if os.path.isfile(p) else []

    def issue_body(self, n):
        p = os.path.join(self.issue_dir(n), "body")
        return self._read(p).rstrip("\n") if os.path.isfile(p) else ""

    # --- comments ---
    def _comment_files(self, d):
        cdir = os.path.join(d, "comments")
        if not os.path.isdir(cdir):
            return []
        return sorted(os.path.join(cdir, e) for e in os.listdir(cdir)
                      if e.endswith(".md"))

    def comments(self, n):
        d = self.issue_dir(n)
        created = self._read(os.path.join(d, "createdAt")).rstrip("\n")
        out = []
        for f in self._comment_files(d):
            at_file = f[:-3] + ".at"
            at = (self._read(at_file).rstrip("\n")
                  if os.path.isfile(at_file) else created)
            # REST spells the field created_at; kraken maps it to createdAt.
            out.append({"body": self._read(f), "created_at": at})
        return out

    def add_comment(self, n, body):
        d = self.issue_dir(n)
        cdir = os.path.join(d, "comments")
        with self.knobs.lock:
            os.makedirs(cdir, exist_ok=True)
            seq = len(self._comment_files(d)) + 1
            self._write(os.path.join(cdir, "%04d.md" % seq), body + "\n")
        return {"id": seq, "body": body}

    # --- labels on an issue ---
    def add_label(self, n, name):
        p = os.path.join(self.issue_dir(n), "labels")
        with self.knobs.lock:
            labels = self._lines(self._read(p)) if os.path.isfile(p) else []
            if name not in labels:
                labels.append(name)
            self._write(p, "".join(l + "\n" for l in labels))

    def remove_label(self, n, name):
        """Remove `name`; return True if it was present (else the caller answers
        404, which kraken tolerates as an idempotent no-op)."""
        p = os.path.join(self.issue_dir(n), "labels")
        with self.knobs.lock:
            labels = self._lines(self._read(p)) if os.path.isfile(p) else []
            if name not in labels:
                return False
            self._write(p, "".join(l + "\n" for l in labels if l != name))
            return True

    # --- graphql node shapes ---
    def blocked_by_nodes(self, n, upper):
        out = []
        bf = os.path.join(self.issue_dir(n), "blocked_by")
        if os.path.isfile(bf):
            for bn in self._lines(self._read(bf)):
                if not bn:
                    continue
                sf = self.path("issues", bn, "state")
                default = "CLOSED" if upper else "closed"
                st = self._read(sf).rstrip("\n") if os.path.isfile(sf) else default
                out.append({"number": int(bn), "state": st.upper() if upper else st})
        return out

    def issue_node(self, n, comment_window=25):
        d = self.issue_dir(n)
        # The queue walk carries a trailing comment window (GraphQL spells the
        # timestamp createdAt where REST spells it created_at) — the requeue
        # derivation reads it off the same page as the labels.
        window = self.comments(n)[-comment_window:] if comment_window else []
        return {
            "number": int(n),
            "title": self._read(os.path.join(d, "title")).rstrip("\n"),
            "createdAt": self._read(os.path.join(d, "createdAt")).rstrip("\n"),
            "body": self.issue_body(n),
            "labels": {"nodes": [{"name": x} for x in self.issue_labels(n)]},
            "comments": {"nodes": [{"body": c["body"], "createdAt": c["created_at"]}
                                   for c in window]},
            "blockedBy": {"nodes": self.blocked_by_nodes(n, upper=True)},
        }

    # --- repo labels ---
    def repo_label_names(self):
        names = set()
        for d in self.issue_dirs():
            for l in self._lines(self._read(os.path.join(d, "labels"))):
                if l:
                    names.add(l)
        reg = self.path("labels")
        if os.path.isfile(reg):
            for l in self._lines(self._read(reg)):
                if l:
                    names.add(l)
        return sorted(names)

    def label_exists(self, name):
        return name in self.repo_label_names()

    def upsert_label(self, name, color, desc):
        with self.knobs.lock:
            reg = self.path("labels")
            existing = self._lines(self._read(reg)) if os.path.isfile(reg) else []
            if name not in existing:
                self._write(reg, "".join(l + "\n" for l in existing + [name]))
            self._write(self.path("labels-meta", name),
                        "color=%s\ndescription=%s\n" % (color, desc))

    # --- claim refs / git data ---
    def ref_path(self, n, gen=0):
        # Flat filenames keep the state dir simple: generation 0 is the bare
        # protocol/5 name, anything else is "<issue>-<gen>".
        name = str(n) if int(gen) == 0 else "%d-%d" % (int(n), int(gen))
        return self.path("refs", name)

    @staticmethod
    def parse_ref_file(name):
        """(issue, generation) for a refs/ filename, or None."""
        if name.isdigit():
            return (int(name), 0)
        issue, _, gen = name.partition("-")
        if issue.isdigit() and gen.isdigit():
            return (int(issue), int(gen))
        return None

    @staticmethod
    def ref_name(n, gen):
        return ("refs/kraken/claims/%d" % n if gen == 0
                else "refs/kraken/claims/%d/%d" % (n, gen))

    def create_commit(self, message):
        sha = hashlib.sha1(
            ("%s\n%d\n%d" % (message, time.time_ns(), os.getpid())).encode("utf-8")
        ).hexdigest()
        with self.knobs.lock:
            self._write(self.path("objects", sha + ".json"),
                        json.dumps({"message": message, "committedDate": _iso_now()}))
        return sha

    def single_ref(self, n):
        f = self.ref_path(n)
        if not os.path.isfile(f):
            return None
        sha = self._read(f).rstrip("\n")
        return {"ref": self.ref_name(int(n), 0), "object": {"sha": sha}}

    def matching_refs(self, prefix=""):
        """GitHub's matching-refs semantics: every ref whose NAME starts with
        "refs/<prefix>" — a plain string prefix, so asking for
        kraken/claims/12 also returns kraken/claims/120/1. The client is
        expected to filter; the stub must not do it for them."""
        rdir = self.path("refs")
        out = []
        if os.path.isdir(rdir):
            entries = []
            for e in os.listdir(rdir):
                parsed = self.parse_ref_file(e)
                if parsed is not None:
                    entries.append((parsed[0], parsed[1], e))
            for n, gen, e in sorted(entries):
                name = self.ref_name(n, gen)
                if not name.startswith("refs/" + prefix):
                    continue
                sha = self._read(os.path.join(rdir, e)).rstrip("\n")
                out.append({"ref": name, "object": {"sha": sha}})
        return out

    # --- pull requests ---
    def pull_request(self, number):
        f = self.path("pr", "%04d.json" % int(number))
        if not os.path.isfile(f):
            return None
        obj = json.loads(self._read(f))
        merged_at = obj.get("mergedAt")
        return {"state": obj.get("state", ""), "merged_at": merged_at,
                "merged": bool(merged_at)}

    # --- contents API ---
    def content_get(self, path):
        cfile = self.path("contents", path)
        if not os.path.isfile(cfile):
            return None
        with open(cfile, "rb") as f:
            data = f.read()
        return {"content": base64.b64encode(data).decode("ascii"),
                "sha": _blob_sha(data), "path": path}

    # --- repo existence / creation ---
    def repo_exists(self):
        return (os.path.isfile(self.path("repo", "nameWithOwner"))
                or os.path.isfile(self.path("repo", "create.json")))

    def create_repo(self, name, private):
        with self.knobs.lock:
            self._write(self.path("repo", "create.json"),
                        json.dumps({"name": name, "private": bool(private)}))
            if not os.path.isfile(self.path("repo", "nameWithOwner")):
                self._write(self.path("repo", "nameWithOwner"), name + "\n")


# --- GraphQL dispatch (the three shapes kraken.py sends) ----------------------

def graphql_list_issues(state, q):
    lm = re.search(r"labels: \[[^\]]*\]", q)
    labels = re.findall(r'"([^"]*)"', lm.group(0)) if lm else []
    fm = re.search(r"first: ([0-9]+)", q)
    first = int(fm.group(1)) if fm else 100
    cm = re.search(r"comments\(last: ([0-9]+)\)", q)
    window = int(cm.group(1)) if cm else 0
    am = re.search(r'after: "([^"]*)"', q)
    after = am.group(1) if am else ""

    nodes = []
    for d in state.issue_dirs():
        n = int(os.path.basename(d))
        if not state.is_open(d):
            continue
        present = state.issue_labels(n)
        if not all(l in present for l in labels):
            continue
        nodes.append(state.issue_node(n, comment_window=window))
    nodes.sort(key=lambda x: (x["createdAt"], x["number"]))
    if after != "":
        after_n = int(after)
        nodes = [x for x in nodes if x["number"] > after_n]
    page = nodes[:first]
    has_next = len(nodes) > first
    end_cursor = str(page[-1]["number"]) if page else ""
    return {"data": {"repository": {"issues": {
        "pageInfo": {"hasNextPage": has_next, "endCursor": end_cursor},
        "nodes": page,
    }}}}


def graphql_issue_states(state, q):
    fields = {}
    for m in re.finditer(r"([a-zA-Z_][a-zA-Z0-9_]*): issue\(number: ([0-9]+)\)", q):
        alias, num = m.group(1), m.group(2)
        d = state.issue_dir(num)
        sf = os.path.join(d, "state")
        st = state._read(sf).rstrip("\n").upper() if os.path.isfile(sf) else "CLOSED"
        names = state.issue_labels(num) if os.path.isdir(d) else []
        fields[alias] = {"state": st, "labels": {"nodes": [{"name": x} for x in names]}}
    return {"data": {"repository": fields}}


def graphql_commit_meta(state, q):
    fields = {}
    for m in re.finditer(r'([a-zA-Z_][a-zA-Z0-9_]*): object\(oid: "([0-9a-f]+)"\)', q):
        alias, sha = m.group(1), m.group(2)
        f = state.path("objects", sha + ".json")
        fields[alias] = json.loads(state._read(f)) if os.path.isfile(f) else None
    return {"data": {"repository": fields}}


def graphql_dispatch(state, q):
    if "issues(states: OPEN" in q:
        return graphql_list_issues(state, q)
    if "object(oid:" in q:
        return graphql_commit_meta(state, q)
    return graphql_issue_states(state, q)


# --- pagination --------------------------------------------------------------

def paginate(items, qs):
    per = int((qs.get("per_page") or ["100"])[0])
    page = int((qs.get("page") or ["1"])[0])
    start = (page - 1) * per
    return items[start:start + per]


# --- HTTP handler ------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # Quiet: the default logs every request to stderr and poisons test output.
    def log_message(self, *args):
        pass

    @property
    def state(self):
        return self.server.state

    @property
    def knobs(self):
        return self.server.knobs

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def _send(self, status, obj=None):
        payload = b"" if obj is None else json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def _log_and_maybe_fail(self, method, raw_path, body):
        """Append the request to the log and, if the fail knob matches, answer
        500. The match subject is "METHOD path" (plus the GraphQL query, so a
        query-body regex like `issue\\(number:` still selects the right call)."""
        from urllib.parse import urlparse
        parsed = urlparse(raw_path)
        line = "%s %s" % (method, raw_path)
        subject = line
        if parsed.path == "/graphql":
            subject = "%s %s" % (line, body.get("query", ""))
        # Content writes record the CAS precondition (the blob sha) on the log
        # line so the upgrade test can prove an update carried it.
        if method == "PUT" and "/contents/" in parsed.path and body.get("sha"):
            line = "%s sha=%s" % (line, body["sha"])
        with self.knobs.lock:
            with open(self.state.path("log"), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        if self.knobs.fail and re.search(self.knobs.fail, subject):
            self._send(500, {"message": "injected failure"})
            return True
        return False

    # BaseHTTPRequestHandler dispatches by method name.
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method):
        from urllib.parse import urlparse, parse_qs, unquote
        # DELETE carries a body on the contents API (the required blob sha), so
        # it is read like the write methods rather than assumed empty.
        body = self._body() if method in ("POST", "PATCH", "PUT", "DELETE") else {}
        if self._log_and_maybe_fail(method, self.path, body):
            return
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        try:
            self._route(method, path, qs, body, unquote)
        except BrokenPipeError:
            pass
        except Exception as exc:  # a stub bug should surface, not hang the client
            self._send(500, {"message": "stub error: %s" % exc})

    def _route(self, method, path, qs, body, unquote):
        s = self.state

        # --- GraphQL ---
        if path == "/graphql" and method == "POST":
            self._send(200, graphql_dispatch(s, body.get("query", "")))
            return

        # --- claim-ref git-data surface ---
        m = re.match(r"^/repos/[^/]+/[^/]+/git/commits$", path)
        if m and method == "POST":
            self._send(201, {"sha": s.create_commit(body.get("message", ""))})
            return

        m = re.match(r"^/repos/[^/]+/[^/]+/git/refs$", path)
        if m and method == "POST":
            self._create_ref(body)
            return

        m = re.match(
            r"^/repos/[^/]+/[^/]+/git/refs/kraken/claims/([0-9]+)(?:/([0-9]+))?$",
            path)
        if m:
            n, gen = int(m.group(1)), int(m.group(2) or 0)
            if method == "PATCH":
                self._update_ref(n, gen, body)
                return
            if method == "DELETE":
                self._delete_ref(n, gen)
                return

        m = re.match(r"^/repos/[^/]+/[^/]+/git/ref/kraken/claims/([0-9]+)$", path)
        if m and method == "GET":
            ref = s.single_ref(int(m.group(1)))
            if ref is None:
                self._send(404, {"message": "Not Found"})
            else:
                self._send(200, ref)
            return

        m = re.match(r"^/repos/[^/]+/[^/]+/git/matching-refs/(.*)$", path)
        if m and method == "GET" and m.group(1).startswith("kraken/claims"):
            self._send(200, paginate(s.matching_refs(m.group(1)), qs))
            return

        # --- issue comments ---
        m = re.match(r"^/repos/[^/]+/[^/]+/issues/([0-9]+)/comments$", path)
        if m and method == "GET":
            self._send(200, paginate(s.comments(m.group(1)), qs))
            return
        if m and method == "POST":
            self._send(201, s.add_comment(m.group(1), body.get("body", "")))
            return

        # --- issue labels (add / remove) ---
        m = re.match(r"^/repos/[^/]+/[^/]+/issues/([0-9]+)/labels$", path)
        if m and method == "POST":
            for name in body.get("labels", []):
                s.add_label(m.group(1), name)
            self._send(200, [{"name": x} for x in s.issue_labels(m.group(1))])
            return
        m = re.match(r"^/repos/[^/]+/[^/]+/issues/([0-9]+)/labels/(.+)$", path)
        if m and method == "DELETE":
            if s.remove_label(m.group(1), unquote(m.group(2))):
                self._send(200, [{"name": x} for x in s.issue_labels(m.group(1))])
            else:
                self._send(404, {"message": "Label does not exist"})
            return

        # --- issues listing by label (open_issue_numbers) ---
        m = re.match(r"^/repos/[^/]+/[^/]+/issues$", path)
        if m and method == "GET":
            self._list_issues_by_label(qs)
            return

        # --- single issue (the claim guard read + label/body/state readers) ---
        m = re.match(r"^/repos/[^/]+/[^/]+/issues/([0-9]+)$", path)
        if m and method == "GET":
            self._issue_detail(m.group(1))
            return

        # --- pulls ---
        m = re.match(r"^/repos/[^/]+/[^/]+/pulls/([0-9]+)$", path)
        if m and method == "GET":
            pr = s.pull_request(m.group(1))
            self._send(200 if pr else 404, pr or {"message": "Not Found"})
            return

        # --- contents API ---
        m = re.match(r"^/repos/[^/]+/[^/]+/contents/(.+)$", path)
        if m:
            self._contents(method, m.group(1), body)
            return

        # --- repo labels (list / create) and label update ---
        m = re.match(r"^/repos/[^/]+/[^/]+/labels$", path)
        if m and method == "GET":
            self._send(200, paginate([{"name": x} for x in s.repo_label_names()], qs))
            return
        if m and method == "POST":
            self._create_label(body)
            return
        m = re.match(r"^/repos/[^/]+/[^/]+/labels/(.+)$", path)
        if m and method == "PATCH":
            s.upsert_label(unquote(m.group(1)), body.get("color", ""),
                           body.get("description", ""))
            self._send(200, {"name": unquote(m.group(1))})
            return

        # --- repo create (POST /user/repos) ---
        if path == "/user/repos" and method == "POST":
            s.create_repo(body.get("name", ""), body.get("private", False))
            self._send(201, {"name": body.get("name", ""),
                             "private": bool(body.get("private"))})
            return

        # --- repo existence (GET /repos/owner/name) ---
        m = re.match(r"^/repos/[^/]+/[^/]+$", path)
        if m and method == "GET":
            if s.repo_exists():
                self._send(200, {"full_name": path[len("/repos/"):], "private": True})
            else:
                self._send(404, {"message": "Not Found"})
            return

        self._send(404, {"message": "stub: unrouted %s %s" % (method, path)})

    # --- handlers needing the state lock / special semantics -----------------

    def _issue_detail(self, n):
        s = self.state
        labels = self.knobs.guard_wait(lambda: s.issue_labels(n))
        self._send(200, {
            "number": int(n),
            # Real GitHub returns the title here; next-action briefs a resumed
            # task off this read, so omitting it made the stub the only place
            # where a resumed brief was title-less.
            "title": s._read(os.path.join(s.issue_dir(n), "title")).rstrip("\n"),
            "labels": [{"name": x} for x in labels],
            "body": s.issue_body(n),
            "state": "open" if s.is_open(s.issue_dir(n)) else "closed",
        })

    def _list_issues_by_label(self, qs):
        s = self.state
        want = (qs.get("labels") or [""])[0]
        want_labels = [l for l in want.split(",") if l]
        out = []
        for d in s.issue_dirs():
            n = int(os.path.basename(d))
            if not s.is_open(d):
                continue
            present = s.issue_labels(n)
            if all(l in present for l in want_labels):
                out.append({"number": n})
        out.sort(key=lambda x: x["number"])
        self._send(200, paginate(out, qs))

    def _create_ref(self, body):
        s = self.state
        ref = body.get("ref", "")
        # Race tests line every creator up here, right before the CAS.
        self.knobs.cas_wait()
        m = re.match(r"^refs/kraken/claims/([0-9]+)(?:/([0-9]+))?$", ref)
        if not m:
            self._send(422, {"message": "unsupported ref name"})
            return
        path = s.ref_path(m.group(1), m.group(2) or 0)
        # The CAS: creation is atomic under the lock — the lock IS GitHub's
        # server-side serialization, so exactly one racer sees "absent".
        with self.knobs.lock:
            if os.path.isfile(path):
                self._send(422, {"message": "Reference already exists"})
                return
            s._write(path, body.get("sha", "") + "\n")
        self._send(201, {"ref": ref})

    def _update_ref(self, n, gen, body):
        s = self.state
        path = s.ref_path(n, gen)
        with self.knobs.lock:
            if not os.path.isfile(path):
                self._send(422, {"message": "Reference does not exist"})
                return
            s._write(path, body.get("sha", "") + "\n")
        self._send(200, {"ref": s.ref_name(n, gen)})

    def _delete_ref(self, n, gen):
        s = self.state
        path = s.ref_path(n, gen)
        with self.knobs.lock:
            if not os.path.isfile(path):
                self._send(422, {"message": "Reference does not exist"})
                return
            os.remove(path)
        self._send(200, {})

    def _contents(self, method, path, body):
        s = self.state
        if method == "GET":
            obj = s.content_get(path)
            self._send(200 if obj else 404, obj or {"message": "Not Found"})
            return
        if method == "PUT":
            cfile = s.path("contents", path)
            with self.knobs.lock:
                existing = os.path.isfile(cfile)
                if existing:
                    with open(cfile, "rb") as f:
                        cur_sha = _blob_sha(f.read())
                    if body.get("sha") != cur_sha:
                        self._send(422, {"message": "sha required/mismatch to update"})
                        return
                elif body.get("sha"):
                    self._send(422, {"message": "sha given for a nonexistent file"})
                    return
                raw = base64.b64decode(body.get("content", "").replace("\n", ""))
                os.makedirs(os.path.dirname(cfile), exist_ok=True)
                with open(cfile, "wb") as f:
                    f.write(raw)
            self._send(200, {"content": {"path": path, "sha": _blob_sha(raw)}})
            return
        if method == "DELETE":
            cfile = s.path("contents", path)
            with self.knobs.lock:
                if not os.path.isfile(cfile):
                    self._send(404, {"message": "Not Found"})
                    return
                with open(cfile, "rb") as f:
                    cur_sha = _blob_sha(f.read())
                # GitHub requires the current blob sha on a delete, exactly as on
                # an update — a mismatch means someone else moved the file.
                if body.get("sha") != cur_sha:
                    self._send(409, {"message": "sha mismatch on delete"})
                    return
                os.remove(cfile)
            self._send(200, {"content": None, "commit": {}})
            return
        self._send(404, {"message": "unrouted contents"})

    def _create_label(self, body):
        s = self.state
        name = body.get("name", "")
        if s.label_exists(name):
            self._send(422, {"message": "already_exists"})
            return
        s.upsert_label(name, body.get("color", ""), body.get("description", ""))
        self._send(201, {"name": name})


def _make_server(state_dir):
    knobs = Knobs()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    httpd.state = StubState(state_dir, knobs)
    httpd.knobs = knobs
    return httpd, "http://127.0.0.1:%d" % httpd.server_address[1], knobs


def start(state_dir):
    """Start the stub in a background thread; return (httpd, base_url, knobs).
    The caller sets GITHUB_API_URL=base_url and stops it via httpd.shutdown().
    Used by the in-process conformance harness (tests/conformance/harness.py)."""
    httpd, url, knobs = _make_server(state_dir)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, url, knobs


if __name__ == "__main__":
    # Standalone entrypoint for the agent-behavior harness (tests/agent), which is
    # bash-driven and cannot share this process: `python3 server.py <state_dir>`
    # prints the chosen base URL on the first stdout line, then serves until
    # killed. No injection knobs — those are a conformance-only concern.
    import sys

    _httpd, _url, _ = _make_server(sys.argv[1])
    sys.stdout.write(_url + "\n")
    sys.stdout.flush()
    try:
        _httpd.serve_forever()
    except KeyboardInterrupt:
        pass
