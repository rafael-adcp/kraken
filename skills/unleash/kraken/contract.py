"""Exit codes, wire types and the diagnostics channel — the vocabulary
every other module speaks. Depends on nothing in the package.

Part of the kraken protocol package; see __init__.py."""
from __future__ import annotations

import contextlib
import json
import os
import sys
from typing import Any, Iterator, TypedDict

# Exit codes — the agent branches on these; keep them identical to the scripts.
EXIT_OK = 0
EXIT_LOST = 10
EXIT_NOT_CLEAR = 11
EXIT_UNKNOWN_PROJECT = 13  # drain refused: the repo has no project:<name> label
EXIT_TRANSPORT = 20
EXIT_NONE = 3  # claim-next: nothing startable to claim (not empty-vs-error ambiguous)
EXIT_USAGE = 2

# --- the vocabulary the signatures below are written in ----------------------
# Aliases, not new types: they name what an int or a str means.
Repo = str          # "OWNER/name" — a coordination or work repo slug
Worker = str        # a worker's declared identity, e.g. "env-1"
Issue = int         # a coordination-repo issue number
Gen = int           # a claim ref's generation (the lease ladder's rung)
Sha = str           # a git object id
Epoch = float       # seconds since the unix epoch, as time.time() reports them

# GitHub payloads stay dicts: foreign data whose shape this program does not own.
Json = dict[str, Any]
Node = Json         # one issue node off the GraphQL queue walk
CommentRecord = Json  # one {author, body, createdAt} record off a thread
CommitMeta = dict[Sha, Json]  # sha -> {committedDate, message}


class ClaimRecord(TypedDict):
    """The `claim-<worker>.json` scratch file. `issue` is a string because
    shell writes the file too."""

    repo: str
    issue: str
    worker: str


class _EnvelopeRequired(TypedDict):
    action: str      # the verdict a consumer branches on
    repo: Repo
    worker: Worker


class Envelope(_EnvelopeRequired, total=False):
    """The JSON object `next-action` prints. A TypedDict because optional keys
    are absent, never null, and the conformance suite pins the bytes."""

    issue: Issue
    resumed: bool
    bounced: bool    # the task came back: a comment landed past its anchor (§6)
    pr: str          # where an earlier turn delivered (§8) — rework continues there
    feedback: list[CommentRecord]  # the human comments past the anchor (§6)
    reason: str      # a stable machine slug — branch on this
    detail: str      # the human sentence — read this, never match on it
    holding: Json    # {"repo", "issue"} of a claim that must be resolved first
    brief: Json      # the task briefing (title / goal / acceptance)
    lease: Json      # the renewal contract: expires_at, renew_every_seconds, …
    then: dict[str, str]  # fully-interpolated commands for the legal next writes


# The operator-facing states that hold a task. `in-progress` is absent on
# purpose: it projects the lease for humans and is never read back (§3).
HELD_LABELS = ("needs-decision", "awaiting-merge")

# A scheduling preference, not a state: offered first, FIFO within each tier.
# An old worker that ignores it falls back to pure FIFO, so no version bump.
PRIORITY_LABEL = "priority:high"

# The revision of PROTOCOL.md this program implements.
PROTOCOL_VERSION = 9

# skills/unleash — the parent of this package.
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The executable a worker is told to run.
ENTRYPOINT = os.path.join(SKILL_DIR, "kraken.py")

# Installed plugin version, single-sourced from the manifest the release workflow
# bumps; read at runtime for the Kraken-Task trailer, never a second literal.
PLUGIN_MANIFEST = os.path.join(
    SKILL_DIR, "..", "..", ".claude-plugin", "plugin.json")
PLUGIN_VERSION_UNKNOWN = "unknown"

# The spec, bundled beside the skill; only ever read a whole section at a time.
PROTOCOL_DOC = os.path.join(SKILL_DIR, "..", "..", "PROTOCOL.md")


def plugin_version(manifest: str = PLUGIN_MANIFEST) -> str:
    """Plugin version from the bundled `.claude-plugin/plugin.json`, or
    ``"unknown"`` if it is missing or unreadable."""
    try:
        with open(manifest, encoding="utf-8") as f:
            version = json.load(f).get("version")
    except (OSError, ValueError):
        return PLUGIN_VERSION_UNKNOWN
    return version if isinstance(version, str) and version else PLUGIN_VERSION_UNKNOWN


def protocol_section(number: int, doc: str = PROTOCOL_DOC) -> list[str]:
    """One numbered PROTOCOL.md section, verbatim, as lines — so a rule reaches
    a subagent unparaphrased. `[]` when it cannot be read: no placeholder text,
    which would read as "here are the rules"."""
    try:
        with open(doc, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    opening = f"## {int(number)}. "
    out: list[str] = []
    for line in lines:
        if out and line.startswith("## "):
            break
        if out or line.startswith(opening):
            out.append(line)
    while out and not out[-1].strip():
        out.pop()
    return out


# --- diagnostic output -------------------------------------------------------
# One-line diagnostics go to stdout, except while `next-action` owns stdout for
# its envelope. Only code next-action reaches goes through `diag`.

_DIAG_STREAM = None  # None -> sys.stdout, resolved per call so tests can capture


def diag(text: str) -> None:
    """Print one diagnostic line to the current diagnostic sink."""
    print(text, file=_DIAG_STREAM if _DIAG_STREAM is not None else sys.stdout)


@contextlib.contextmanager
def diagnostics_on_stderr() -> Iterator[None]:
    """Route `diag` to stderr while stdout carries a machine payload."""
    global _DIAG_STREAM
    previous = _DIAG_STREAM
    _DIAG_STREAM = sys.stderr
    try:
        yield
    finally:
        _DIAG_STREAM = previous


# The generation of a bare refs/kraken/claims/<issue> (protocol/5): never
# created any more, just the lowest rung, so generation 1 supersedes it.
LEGACY_CLAIM_GEN = 0
