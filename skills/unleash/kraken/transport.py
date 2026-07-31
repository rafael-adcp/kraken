"""The GitHub boundary: one object, one network touchpoint.

Part of the kraken protocol package; see __init__.py."""
from __future__ import annotations

import base64
import datetime
import email.utils
import json
import os
import re
import subprocess
import time
import urllib.request
from typing import Any, Sequence

from .contract import CommentRecord, Epoch, Issue, Json, Repo, Sha

# --- transport ---------------------------------------------------------------
# Direct HTTP over stdlib urllib, behind ONE object. `Api.request` is the single
# network touchpoint; everything else in the class builds on it, and everything
# above the class is handed an `Api` rather than reaching for a module global. A
# non-2xx answer is an integer status the caller branches on — HTTP 422 is the
# CAS-lost signal on the claim surface, everything else non-2xx is the exit-20
# transport-failure path.
#
# The object also carries the repo, because every call above the boundary was
# threading it through as a first parameter anyway: `repo` is not an argument of
# each request, it is which server-side thing this client talks to. Functions
# that merely NAME a repo in text (a comment trailer, a state file, a command
# line the worker will run) still take a plain `Repo` — they make no call, so
# they get no client.

DEFAULT_API_URL = "https://api.github.com"
HTTP_TIMEOUT_SECONDS = 30
# Sentinel for "no HTTP answer at all" (DNS, refused, timeout). Not a real
# status code, so every `status == N` decision reads it as a transport fault.
STATUS_NETWORK_FAILURE = 0
# GitHub caps per_page at 100; a page shorter than this is the last one.
PER_PAGE = 100

# How many aliased fields one batched GraphQL call carries. Every fan-out in this
# program — claim commit meta, depends-on targets, comment windows — is one
# aliased field per item inside a single query, which is what keeps a fan-out
# from becoming a request per item. Uncapped, that turns into the opposite
# problem: the query grows one field per claim with no ceiling, and an over-large
# query does not degrade gracefully — the server rejects it whole, so every item
# in it fails together. 100 is the page size the issue walk already uses.
GRAPHQL_ALIAS_CHUNK = 100

_TOKEN_CACHE = {"resolved": False, "token": ""}


def api_base() -> str:
    """The API root every request is made against. GITHUB_API_URL is both the
    GitHub Actions convention (set on every runner, GHES included) and the
    conformance seam (tests point it at a local stub server)."""
    return (os.environ.get("GITHUB_API_URL") or DEFAULT_API_URL).rstrip("/")


def github_token() -> str:
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


def quote_path(segment: str) -> str:
    """URL-quote one path segment (a label name, a contents path component)."""
    return urllib.parse.quote(segment, safe="")


def parse_http_date(value: str) -> Epoch | None:
    """An HTTP `Date` header to epoch seconds, or None when it is missing or
    unparseable. RFC 9110 allows three spellings and `parsedate_to_datetime`
    reads all three; a value that carries no zone is read as UTC, which is what
    the only legal spelling ("...GMT") means anyway."""
    if not value:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp()


def comment_total_of(issue_obj: Json) -> int | None:
    """The total comment count off a REST issue object, or None when the field
    is missing or not a number.

    Kept apart from `Api.comment_count` because the queue walk decodes the same
    fact out of GraphQL's `comments { totalCount }` shape, and a count that
    silently reads as 0 would requeue every held task at once. None means "not
    answered" and every caller treats it as a failed read, never as zero."""
    value = issue_obj.get("comments")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return max(0, value)


class Api:
    """Every GitHub call this program makes, against one coordination repo.

    One object instead of a dozen module-level functions, for two reasons. It
    isolates the one thing here that is genuinely external and genuinely
    vulnerable — the network — so the code above it depends on an argument it
    is handed rather than on a global it reaches for. And it gives `repo` a
    home: it is the context every call shares, not the first parameter of each
    of them.

    A test builds a stand-in and passes it in. Nothing rebinds a module
    attribute to fake a request, so no test can leak a fake into another by
    forgetting to restore it, and reading a call site tells you what it talks
    to."""

    def __init__(self, repo: Repo = ""):
        self.repo = repo
        # How far this machine's clock is from the server's, in seconds, or None
        # until a response has been seen. See `server_now`.
        self._clock_offset: float | None = None

    # --- the boundary --------------------------------------------------------

    def request(self, method: str, path: str,
                body: Json | None = None) -> tuple[int, str]:
        """One API call: return (status, text). `path` starts with "/"; a dict
        `body` is sent as JSON. Never raises — a request that got no HTTP answer
        at all comes back as (STATUS_NETWORK_FAILURE, ""), and a non-2xx answer
        keeps its real integer status so the caller can tell a CAS-lost 422 from
        a fault."""
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
                self._note_server_clock(resp.headers)
                return resp.getcode(), resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            # An error answer is still an answer, and its Date is as good as a
            # 2xx's — a run whose every call 404s still gets the server's clock.
            self._note_server_clock(exc.headers)
            try:
                text = exc.read().decode("utf-8", "replace")
            except OSError:
                text = ""
            return exc.code, text
        except (urllib.error.URLError, OSError, ValueError):
            return STATUS_NETWORK_FAILURE, ""

    # --- the server's clock --------------------------------------------------

    def _note_server_clock(self, headers) -> None:
        """Record the offset between this machine's clock and the server's, from
        the `Date` header every HTTP response carries. An absent or unparseable
        Date leaves the last known offset alone — one header GitHub omitted must
        not throw away a reading a hundred earlier ones agreed on."""
        epoch = parse_http_date(headers.get("Date") or "") if headers else None
        if epoch is not None:
            self._clock_offset = epoch - time.time()

    def server_now(self) -> Epoch:
        """Now, on the SERVER's clock — the `now` every lease expiry is decided
        against (PROTOCOL.md §5.1).

        The lease timestamp is a commit date GitHub stamped, so comparing it to
        `time.time()` compares two clocks and calls the difference age: a worker
        running five minutes fast steals live leases, one running five minutes
        slow keeps dead ones alive. Reading it costs no request — the answer
        rides the `Date` header of every response the process already made.

        It is stored as an OFFSET rather than as an instant, because a driver
        that supervises a task for an hour asks this repeatedly and a frozen
        epoch would answer the same second forever. Local time still ticks; this
        only corrects where it starts. Before the first response — and against a
        server that sends no Date — the offset is unknown and this is plain
        `time.time()`, which is exactly the behaviour that predates it."""
        return time.time() + (self._clock_offset or 0.0)

    def json(self, method: str, path: str,
             body: Json | None = None) -> Any | None:
        """Parsed JSON from a 2xx response, or None on any failure (non-2xx,
        network fault, undecodable body) — the exit-20 mapping for plain reads."""
        status, text = self.request(method, path, body)
        if not 200 <= status < 300:
            return None
        try:
            return json.loads(text)
        except (ValueError, json.JSONDecodeError):
            return None

    def paginated(self, path: str) -> list[Json] | None:
        """Every element of a paginated list endpoint, concatenated across
        per_page/page requests (a page shorter than PER_PAGE is the last).
        Returns a list, or None on transport failure."""
        sep = "&" if "?" in path else "?"
        items = []
        page = 1
        while True:
            chunk = self.json("GET", f"{path}{sep}per_page={PER_PAGE}&page={page}")
            if not isinstance(chunk, list):
                return None
            items.extend(chunk)
            if len(chunk) < PER_PAGE:
                return items
            page += 1

    def graphql(self, query: str) -> Json | None:
        """POST a GraphQL query; return the parsed {"data": ...} envelope, or
        None on transport / decode / GraphQL-error failure."""
        resp = self.json("POST", "/graphql", {"query": query})
        if not isinstance(resp, dict) or resp.get("errors"):
            return None
        if not isinstance(resp.get("data"), dict):
            return None
        return resp

    def aliased(self, fields: Sequence[str],
                chunk: int = GRAPHQL_ALIAS_CHUNK) -> Json | None:
        """The batched fan-out: `fields` are pre-aliased selections on
        `repository`, sent as ONE query per `chunk` of them, with the answers
        merged into a single `{alias: node}` object. Returns that object — `{}`
        for an empty ask, without a request — or None on transport failure.

        The one place the fan-out shape lives. Its three callers (claim commit
        meta, depends-on targets, comment windows) differ only in the field they
        build per item and in how many of those fit one call, so the aliasing
        stays with each of them: the caller is what reads the answer back out,
        and only the caller knows whether `c3` names the fourth SHA or `i3`
        names issue 3. Aliases must therefore be unique across the WHOLE ask,
        not just within a chunk, or a merge silently loses one of them.

        A chunk that fails takes the whole read down rather than answering the
        chunks that landed: a caller handed a partial dict cannot tell an item
        the server omitted from one this never asked about, and all three treat
        None as the exit-20 transport path."""
        if not fields:
            return {}
        owner, name = self.repo.split("/", 1)
        merged: Json = {}
        for start in range(0, len(fields), chunk):
            body = " ".join(fields[start:start + chunk])
            resp = self.graphql(
                f'{{ repository(owner: "{owner}", name: "{name}") {{ {body} }} }}')
            if resp is None:
                return None
            merged.update(resp["data"]["repository"] or {})
        return merged

    # --- issues, comments, labels --------------------------------------------

    def comment_records(self, issue: Issue) -> list[CommentRecord] | None:
        """Every comment as a {"body", "createdAt"} record, in server order —
        paginated past 100 via the REST comments endpoint (a single-page read
        would silently truncate a long thread). `status` and the validator's
        debounce read through here so no consumer ever sees a truncated history.
        REST spells the timestamp `created_at`; it is mapped to the createdAt
        shape every consumer reads. Returns a list, or None on transport
        failure."""
        items = self.paginated(f"/repos/{self.repo}/issues/{issue}/comments")
        if items is None:
            return None
        return [
            {"body": c.get("body") or "", "createdAt": c.get("created_at") or ""}
            for c in items if isinstance(c, dict)
        ]

    def post_comment(self, issue: Issue, body: str) -> bool:
        status, _text = self.request(
            "POST", f"/repos/{self.repo}/issues/{issue}/comments", {"body": body}
        )
        return 200 <= status < 300

    def swap_labels(self, issue: Issue, remove: str | None = None,
                    add: str | None = None) -> bool:
        """Remove and/or add one label on an issue. A remove of a label the
        issue no longer carries (HTTP 404) counts as success, so the swap stays
        idempotent under retries and reaper races — the same tolerance `gh issue
        edit --remove-label` had."""
        if remove:
            status, _text = self.request(
                "DELETE",
                f"/repos/{self.repo}/issues/{issue}/labels/{quote_path(remove)}",
            )
            if not (200 <= status < 300 or status == 404):
                return False
        if add:
            status, _text = self.request(
                "POST", f"/repos/{self.repo}/issues/{issue}/labels",
                {"labels": [add]}
            )
            if not 200 <= status < 300:
                return False
        return True

    def issue_detail(self, issue: Issue) -> Json | None:
        """The live issue object (labels, body, state, comment count, ...), or
        None on transport failure — one GET serves the label, body and count
        readers."""
        return self.json("GET", f"/repos/{self.repo}/issues/{issue}")

    def issue_label_names(self, issue: Issue) -> list[str] | None:
        """The label names currently on an issue, live. Returns a list, or None
        on transport failure — the coordination workflows read labels off the
        live issue (a labeled/edited event may have just changed them)."""
        obj = self.issue_detail(issue)
        if obj is None:
            return None
        return [lbl.get("name", "") for lbl in obj.get("labels", [])]

    def comment_count(self, issue: Issue) -> int | None:
        """The issue's total comment count — the anchor a transition writes into
        the state record (PROTOCOL.md §3.1). Returns None on transport failure.

        Read BACK from the issue after the transition's own comment landed, never
        computed as `+1` on a count read before it: a comment that arrives in
        between would otherwise be swallowed by the record, and swallowing an
        operator's words is the one failure the requeue derivation must not have.
        It rides `issue_detail`, so a caller that also wants the labels — the
        claim guard — pays for one request, not two."""
        obj = self.issue_detail(issue)
        if obj is None:
            return None
        return comment_total_of(obj)

    def issue_body(self, issue: Issue) -> str | None:
        """The issue's body text, live. Returns a string ("" when the body is
        empty or null), or None on transport failure."""
        obj = self.issue_detail(issue)
        if obj is None:
            return None
        return obj.get("body") or ""

    def label_upsert(self, name: str, color: str, description: str) -> bool:
        """Upsert a label with its canonical color/description — a create on a
        fresh repo (POST /labels), an in-place re-canonicalize on a re-run
        (PATCH the existing label when the create answers 422 'already_exists').
        This is the `gh label create --force` behaviour over the REST label
        surface."""
        body = {"name": name, "color": color, "description": description}
        status, _ = self.request("POST", f"/repos/{self.repo}/labels", body)
        if status == 422:
            status, _ = self.request(
                "PATCH", f"/repos/{self.repo}/labels/{quote_path(name)}",
                {"new_name": name, "color": color, "description": description},
            )
        return 200 <= status < 300

    # --- the repo itself, and files in it ------------------------------------

    def repo_exists(self) -> bool:
        """True iff the coordination repo already exists (a 2xx from the repo
        GET)."""
        status, _ = self.request("GET", f"/repos/{self.repo}")
        return 200 <= status < 300

    def repo_create_private(self) -> bool:
        """Create the coordination repo PRIVATE — never public: the queue is
        instructions that run in a worker's environment with its credentials.
        Created under the authenticated user (the coordination repo is personal
        by design)."""
        name = self.repo.split("/", 1)[1] if "/" in self.repo else self.repo
        status, _ = self.request(
            "POST", "/user/repos", {"name": name, "private": True})
        return 200 <= status < 300

    def get_content_meta(self, path: str) -> tuple[str | None, Sha | None]:
        """(bytes, blob_sha) for `path` on the repo via the contents API, or
        (None, None) when it is absent (404) OR unreadable. The blob sha is what
        the contents API requires to *update* an existing file — a create never
        sends one. The caller cannot tell 404 from a transport fault here and,
        like the skill, treats 'no readable file' as absent."""
        obj = self.json("GET", f"/repos/{self.repo}/contents/{path}")
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

    def put_content(self, path: str, data: str, message: str) -> bool:
        """CREATE `path` on the repo with `data` via the contents API. Callers
        reach here only when the file was reported absent — an overwrite would
        need the current blob sha, and init never overwrites."""
        body = {"message": message,
                "content": base64.b64encode(data).decode("ascii")}
        status, _ = self.request(
            "PUT", f"/repos/{self.repo}/contents/{path}", body)
        return 200 <= status < 300

    def delete_content(self, path: str, sha: Sha, message: str) -> bool:
        """Delete `path` on the repo via the contents API. The current blob
        `sha` is required (same as an update) — GitHub rejects a delete without
        it, which is what keeps the prune from racing a concurrent edit. True on
        success."""
        status, _ = self.request(
            "DELETE", f"/repos/{self.repo}/contents/{path}",
            {"message": message, "sha": sha},
        )
        return 200 <= status < 300
