#!/usr/bin/env bash
# stop-failure-release.sh — the bundled StopFailure hook (registered in
# hooks/hooks.json with matcher `rate_limit`).
#
# A usage limit kills the turn, not the session: the model stops mid-drain, the
# session sits open waiting for input, and SessionEnd never fires — so a held
# claim would squat on in-progress until the ~6h reaper, and the wake the
# watcher spent on the dead turn would be lost for good (the queue snapshot no
# longer changes, so the watcher would stay silent even after the limit
# resets). StopFailure is the hook event that DOES fire there — Claude Code
# raises it when a turn ends on an API error — so this hook:
#
#   1. stamps the wake-retry flag (${KRAKEN_STATE_DIR:-$HOME/.kraken}/wake-retry).
#      `kraken.py watch` re-emits its wake line when the flag is newer than its
#      last emission, so the drain resumes on its own once the limit window
#      resets — retries during the window die for free, the first one after it
#      lands;
#   2. releases every open claim on this machine (`kraken.py release`, reason
#      "usage limit"): the task requeues in seconds, free for any worker with
#      quota, and this worker re-claims it after reset via the retry wake and
#      continues on the existing branch with the whole thread as context.
#
# Cross-session note: hooks fire in EVERY session running this plugin, not just
# workers. That is why the matcher is `rate_limit` only: a usage limit is
# account-wide, so any session's rate_limit failure implies the worker sessions
# on this account are limited too — releasing their claims is correct, not
# collateral damage. Per-session error types (invalid_request, server_error,
# ...) could fire on a healthy worker's machine and must not release its live
# claim, so they are deliberately not matched.
#
# Best-effort by contract: this ALWAYS exits 0 (Claude Code ignores StopFailure
# output anyway; the discipline is for standalone/test runs). gh still works at
# limit time — only the model API is blocked — so the release normally lands;
# if it fails regardless, the reaper remains the backstop. Claude Code feeds
# the StopFailure JSON event on stdin; we don't need it — the matcher already
# scoped the error type.
set -u

. "$(dirname "$0")/lib-release-claims.sh"

# Flag first, release second: even with no claim held (the limit hit between
# tasks, or on a wake turn), the consumed wake must be retried — and if the
# release fails, the retry still has to happen. mtime is the signal the
# watcher reads; the timestamp content is for humans debugging.
mkdir -p "$KRAKEN_STATE" 2>/dev/null || true
date -u +"%Y-%m-%dT%H:%M:%SZ" > "$KRAKEN_STATE/wake-retry" 2>/dev/null || true

release_all_claims "usage limit"

exit 0
