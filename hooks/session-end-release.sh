#!/usr/bin/env bash
# session-end-release.sh — the bundled SessionEnd hook (registered in
# hooks/hooks.json).
#
# When a worker's Claude Code session ends *gracefully* (terminal closed,
# /exit) while it still holds an open claim, this releases that claim
# automatically: the task returns to the queue in seconds instead of waiting
# ~6h for the reaper. It runs `kraken.py release` (via the shared loop in
# lib-release-claims.sh), so the `released` marker lands before in-progress
# drops, and the claim ref last — the ordering that frees the lock honestly (§9).
#
# Scope, honestly: this covers a graceful end only. A Claude usage limit does
# NOT end the session (the turn aborts, the session stays open waiting for
# input), so SessionEnd never fires — that path is covered by the StopFailure
# hook (stop-failure-release.sh) instead. A hard kill / crash / power loss
# fires neither hook; the reaper stays the backstop for those. See the #60 FAQ
# in README.md.
#
# Best-effort by contract: this ALWAYS exits 0. A failed release just falls back
# to the reaper; session exit must never be blocked. Claude Code feeds a
# SessionEnd JSON event on stdin; we don't need it, so we ignore it.
set -u

. "$(dirname "$0")/lib-release-claims.sh"

release_all_claims "session ended"

exit 0
