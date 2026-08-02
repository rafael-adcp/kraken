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
# Names for the values that travel furthest, so a reader can tell an issue
# number from a generation and a repo slug from a worker name without tracing
# the call. These are aliases, not new types: nothing enforces them at runtime.
Repo = str          # "OWNER/name" — a coordination or work repo slug
Worker = str        # a worker's declared identity, e.g. "env-1"
Issue = int         # a coordination-repo issue number
Gen = int           # a claim ref's generation (the lease ladder's rung)
Sha = str           # a git object id
Epoch = float       # seconds since the unix epoch, as time.time() reports them

# GitHub payloads cross the boundary as decoded JSON and are consumed
# key-by-key. They stay dicts on purpose: they are foreign data this program
# does not own the shape of, and pinning them to a class would only move the
# guesswork.
Json = dict[str, Any]
Node = Json         # one issue node off the GraphQL queue walk
CommentRecord = Json  # one {author, body, createdAt} record off a thread
CommitMeta = dict[Sha, Json]  # sha -> {committedDate, message}


class ClaimRecord(TypedDict):
    """The `claim-<worker>.json` scratch file, as `open_claim_record` returns
    it. `issue` is a STRING because the file is written by shell as well as by
    this program; the readers that need arithmetic convert at the edge."""

    repo: str
    issue: str
    worker: str


class _EnvelopeRequired(TypedDict):
    action: str      # the verdict a consumer branches on
    repo: Repo
    worker: Worker


class Envelope(_EnvelopeRequired, total=False):
    """The single JSON object `next-action` prints on stdout.

    A TypedDict rather than a dataclass on purpose: this IS the wire format,
    every optional key is present only when it has meaning, and the conformance
    suite pins the emitted bytes. A class with defaults would serialize absent
    keys as nulls and change that.
    """

    issue: Issue
    resumed: bool
    bounced: bool    # the task came back: a comment landed past its anchor (§6)
    pr: str          # where an earlier turn delivered (§8) — rework continues there
    reason: str      # a stable machine slug — branch on this
    detail: str      # the human sentence — read this, never match on it
    holding: Json    # {"repo", "issue"} of a claim that must be resolved first
    brief: Json      # the task briefing (title / goal / acceptance)
    lease: Json      # the renewal contract: expires_at, renew_every_seconds, …
    then: dict[str, str]  # fully-interpolated commands for the legal next writes


class ReconcileAction(TypedDict, total=False):
    """One repair `reconcile_plan` decided on and `apply_reconcile` executes.
    `held` rides only the `reclaim` rule (whether to also clear a stale badge),
    and `state`/`comments` only `migrate` (the record its label implies)."""

    rule: str        # "orphan-lock" | "orphan-state" | "reclaim" | "migrate"
                     # | "re-anchor"
    issue: Issue
    reason: str
    gens: list[Gen]  # every rung to delete
    held: bool
    state: str       # the held state a `migrate` writes down
    comments: int    # the anchor that goes with it

# A task carrying either of these is held, never startable. Both are
# operator-facing states with no lock behind them, which is exactly why a label
# can decide them: the label IS the state.
#
# `in-progress` is deliberately absent. It is the claim ref's projection, and a
# projection read by a decision is a second source of truth that then has to be
# reconciled with the first. It is WRITE-ONLY: written for the human reading the
# issue list, never consulted by the claim guard, the startable filter, the
# requeue derivation or the console. The lease alone says whether somebody is
# working, so a label that lags it costs nothing but a stale badge.
HELD_LABELS = ("needs-decision", "awaiting-merge")

# A scheduling preference, not a state: a startable task carrying this label is
# offered ahead of normal ones (createdAt FIFO still breaks ties within each
# tier). Not a coordination invariant — an old worker that ignores it simply
# falls back to pure FIFO, so honoring it needs no PROTOCOL_VERSION bump.
PRIORITY_LABEL = "priority:high"

# The revision of PROTOCOL.md this program implements. Bumping it declares an
# incompatible change to what the refs mean; the spec is the contract, and
# HISTORY.md carries the reasoning behind each revision.
PROTOCOL_VERSION = 9

# Where the skill lives on disk. This file sits one level INSIDE the skill
# directory (skills/unleash/kraken/contract.py), so the anchor is the parent of
# the package — the directory the bundled assets and the `then` commands are
# resolved against.
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The executable a worker is told to run. The package is the implementation; the
# entry point is still `skills/unleash/kraken.py`, so every envelope this program
# emits names the path that has always worked.
ENTRYPOINT = os.path.join(SKILL_DIR, "kraken.py")

# Installed plugin version, single-sourced from the manifest the release workflow
# bumps; read at runtime for the Kraken-Task trailer, never a second literal.
PLUGIN_MANIFEST = os.path.join(
    SKILL_DIR, "..", "..", ".claude-plugin", "plugin.json")
PLUGIN_VERSION_UNKNOWN = "unknown"

# The spec, bundled beside the skill. It is normative prose, so nothing here
# parses it — the one thing read out of it is a whole section, verbatim, for a
# caller that has to hand the rules to something that cannot read a file.
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
    """One numbered PROTOCOL.md section — heading included, trailing blank lines
    trimmed — as lines, or `[]` when the spec cannot be read or carries no such
    section.

    The alternative was asking the model to paste the prose it remembers, which
    is how a rule ends up paraphrased at exactly the moment it matters. The
    delimiter is the spec's own `## <n>.` heading, so a section that grows needs
    no second edit here.

    Empty is not a fallback text on purpose: a caller handing these lines to a
    subagent with push access must be able to tell "here are the rules" from
    "the rules did not load", and any placeholder prose would read as the
    former."""
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
# Every transition prints one-line diagnostics a human (or a shell) reads off
# stdout: "claim: held issue=7 label=needs-decision", "reap: reclaimed issue=9 …".
# `next-action` (below) owns stdout for its JSON envelope, so it routes those
# same lines to stderr for the duration of its run — identical text, identical
# order, just off the machine channel. Only the functions next-action reaches
# through (the claim path and the reconcile applier) go through `diag`; a
# subcommand that never runs inside it keeps printing directly.

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
# Generation of a bare refs/kraken/claims/<issue> — the shape protocol/5 wrote.
# It is never created any more, only read and superseded: an old ref is simply
# the lowest possible generation, so a protocol/5 claim is stolen by creating
# generation 1 over it, with no migration step.
LEGACY_CLAIM_GEN = 0
