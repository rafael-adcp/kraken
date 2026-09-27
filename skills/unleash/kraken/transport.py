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
# Stdlib urllib behind one object; `Api.request` is the only network call. A
# non-2xx answer keeps its integer status: 422 is the CAS-lost signal, anything
# else is the exit-20 path.

DEFAULT_API_URL = "https://api.github.com"
HTTP_TIMEOUT_SECONDS = 30
# Sentinel for "no HTTP answer at all" (DNS, refused, timeout). Not a real
# status code, so every `status == N` decision reads it as a transport fault.
STATUS_NETWORK_FAILURE = 0
# GitHub caps per_page at 100; a page shorter than this is the last one.
PER_PAGE = 100

# Aliased fields per batched GraphQL call: an over-large query is rejected
# whole, so fan-outs are chunked.
GRAPHQL_ALIAS_CHUNK = 100

_TOKEN_CACHE = {"resolved": False, "token": ""}


def api_base() -> str:
    """The API root: GITHUB_API_URL (Actions, GHES, the test stub) or GitHub."""
    return (os.environ.get("GITHUB_API_URL") or DEFAULT_API_URL).rstrip("/")


def github_token() -> str:
    """GH_TOKEN, then GITHUB_TOKEN, then one `gh auth token` spawn, memoized.
    Empty when nothing yields a token."""
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
    """An HTTP `Date` header to epoch seconds (UTC), or None."""
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
    """The comment count off a REST issue object, or None when missing — never
    0, which would sit below every anchor and bury requeues."""
    value = issue_obj.get("comments")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return max(0, value)


class Api:
    """Every GitHub call this program makes, against one coordination repo.
    Handed to callers rather than reached for, so tests pass a stand-in."""

    def __init__(self, repo: Repo = ""):
        self.repo = repo
        self._clock_offset: float | None = None

    # --- the boundary --------------------------------------------------------

    def request(self, method: str, path: str,
                body: Json | None = None) -> tuple[int, str]:
        """One API call: (status, text). Never raises; no HTTP answer at all is
        (STATUS_NETWORK_FAILURE, "")."""
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
            # An error answer's Date is as good as a 2xx's.
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
        """Record our offset from the server's clock; a missing Date keeps the
        last known one."""
        epoch = parse_http_date(headers.get("Date") or "") if headers else None
        if epoch is not None:
            self._clock_offset = epoch - time.time()

    def server_now(self) -> Epoch:
        """Now on the SERVER's clock, which stamped the lease dates (§5.1): a
        skewed local clock would steal live leases or keep dead ones. Stored as
        an offset so it keeps ticking; plain `time.time()` before any response."""
        return time.time() + (self._clock_offset or 0.0)

    def json(self, method: str, path: str,
             body: Json | None = None) -> Any | None:
        """Parsed JSON from a 2xx response, or None on any failure."""
        status, text = self.request(method, path, body)
        if not 200 <= status < 300:
            return None
        try:
            return json.loads(text)
        except (ValueError, json.JSONDecodeError):
            return None

    def paginated(self, path: str) -> list[Json] | None:
        """Every element of a paginated list endpoint, or None on failure."""
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
        """The parsed {"data": ...} envelope, or None on any failure."""
        resp = self.json("POST", "/graphql", {"query": query})
        if not isinstance(resp, dict) or resp.get("errors"):
            return None
        if not isinstance(resp.get("data"), dict):
            return None
        return resp

    def aliased(self, fields: Sequence[str],
                chunk: int = GRAPHQL_ALIAS_CHUNK) -> Json | None:
        """The batched fan-out: pre-aliased `repository` selections, one query
        per `chunk`, merged into `{alias: node}` (`{}` for an empty ask). Aliases
        must be unique across the whole ask. One failed chunk fails it all: a
        partial answer could not be told from an omitted item."""
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
        """Every comment as {"body", "createdAt"}, in server order and fully
        paginated, or None on transport failure."""
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
        """Remove and/or add one label; removing an absent one (404) succeeds."""
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
        """The live REST issue object, or None on transport failure."""
        return self.json("GET", f"/repos/{self.repo}/issues/{issue}")

    def issue_label_names(self, issue: Issue) -> list[str] | None:
        """The issue's live label names, or None on transport failure."""
        obj = self.issue_detail(issue)
        if obj is None:
            return None
        return [lbl.get("name", "") for lbl in obj.get("labels", [])]

    def comment_count(self, issue: Issue) -> int | None:
        """The live comment count, the anchor a transition records (§3.1), or
        None. Callers read it AFTER their own comment lands, never as `+1`: a
        comment arriving in between would otherwise be swallowed."""
        obj = self.issue_detail(issue)
        if obj is None:
            return None
        return comment_total_of(obj)

    def issue_body(self, issue: Issue) -> str | None:
        """The live body ("" when empty), or None on transport failure."""
        obj = self.issue_detail(issue)
        if obj is None:
            return None
        return obj.get("body") or ""

    def label_upsert(self, name: str, color: str, description: str) -> bool:
        """Create a label, or PATCH it back to canonical when it exists (422)."""
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
        """Whether the coordination repo exists."""
        status, _ = self.request("GET", f"/repos/{self.repo}")
        return 200 <= status < 300

    def authenticated_login(self) -> str | None:
        """The login the token authenticates as (`GET /user`), or None when it
        could not be read — a transport fault, never a verdict on ownership."""
        obj = self.json("GET", "/user")
        login = obj.get("login") if isinstance(obj, dict) else None
        return login if isinstance(login, str) and login else None

    def repo_create_private(self) -> bool:
        """Create the coordination repo, always PRIVATE: the queue is
        instructions run with a worker's credentials. `POST /user/repos` ignores
        the slug's owner, so init checks it first (#174)."""
        name = self.repo.split("/", 1)[1] if "/" in self.repo else self.repo
        status, _ = self.request(
            "POST", "/user/repos", {"name": name, "private": True})
        return 200 <= status < 300

    def get_content_meta(self, path: str) -> tuple[str | None, Sha | None]:
        """(bytes, blob_sha) for `path`, or (None, None) when absent OR
        unreadable — both read as absent."""
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
        """Create `path`; init never overwrites, so no blob sha is sent."""
        body = {"message": message,
                "content": base64.b64encode(data).decode("ascii")}
        status, _ = self.request(
            "PUT", f"/repos/{self.repo}/contents/{path}", body)
        return 200 <= status < 300

    def delete_content(self, path: str, sha: Sha, message: str) -> bool:
        """Delete `path` at blob `sha`, so a concurrent edit is never lost."""
        status, _ = self.request(
            "DELETE", f"/repos/{self.repo}/contents/{path}",
            {"message": message, "sha": sha},
        )
        return 200 <= status < 300
