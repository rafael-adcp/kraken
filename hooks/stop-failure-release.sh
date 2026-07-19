#!/usr/bin/env bash
# stop-failure-release.sh — the bundled StopFailure hook (hooks.json, matcher
# `rate_limit`).
#
# A usage limit kills the turn but not the session, so SessionEnd never fires and
# a held claim would squat on in-progress until the ~6h reaper — and the wake the
# watcher spent on the dead turn would be lost (the queue snapshot no longer
# changes). StopFailure is the one event that DOES fire there, so this hook:
#   1. stamps the wake-retry flag ($KRAKEN_STATE_DIR/wake-retry); `kraken.py
#      watch` re-emits its wake when the flag is newer than its last emission, so
#      the drain resumes on its own once the limit window resets;
#   2. releases every open claim on this machine (reason "usage limit"): the task
#      requeues in seconds, and this worker re-claims it after reset via the
#      retry wake and continues on the existing branch.
#
# Matcher is `rate_limit` only: a usage limit is account-wide, so any session's
# rate_limit failure implies the worker sessions here are limited too — releasing
# their claims is correct. Per-session error types could fire on a healthy worker
# and must not release its live claim, so they are deliberately not matched.
#
# Best-effort: ALWAYS exits 0. gh still works at limit time, so the release
# normally lands; if not, the reaper is the backstop. The StopFailure JSON on
# stdin is unused — the matcher already scoped the error type.
set -u

. "$(dirname "$0")/lib-release-claims.sh"

# Flag first, release second: even with no claim held, the consumed wake must be
# retried — and if the release fails, the retry still has to happen. mtime is the
# signal the watcher reads; the timestamp content is for humans.
mkdir -p "$KRAKEN_STATE" 2>/dev/null || true
date -u +"%Y-%m-%dT%H:%M:%SZ" > "$KRAKEN_STATE/wake-retry" 2>/dev/null || true

release_all_claims "usage limit"

exit 0
