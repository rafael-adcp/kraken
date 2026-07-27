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
    reason: str      # a stable machine slug — branch on this
    detail: str      # the human sentence — read this, never match on it
    holding: Json    # {"repo", "issue"} of a claim that must be resolved first
    brief: Json      # the task briefing (title / goal / acceptance)
    lease: Json      # the renewal contract: expires_at, renew_every_seconds, …
    then: dict[str, str]  # fully-interpolated commands for the legal next writes


class ReconcileAction(TypedDict, total=False):
    """One repair `reconcile_plan` decided on and `apply_reconcile` executes.
    `held` rides only the `reclaim` rule (whether to also clear a stale badge)."""

    rule: str        # "orphan-lock" | "reclaim"
    issue: Issue
    reason: str
    gens: list[Gen]  # every rung to delete
    held: bool

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

# Where the skill lives on disk. This file sits one level INSIDE the skill
# directory (skills/unleash/kraken/contract.py), so the anchor is the parent of
# the package — the same path the single-file module used to resolve to, which
# is what keeps the bundled assets and the `then` commands byte-identical.
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


def plugin_version(manifest: str = PLUGIN_MANIFEST) -> str:
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
