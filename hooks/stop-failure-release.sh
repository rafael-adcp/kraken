#!/usr/bin/env bash
# stop-failure-release.sh — the bundled StopFailure hook (hooks.json, matcher
# `rate_limit`).
#
# A usage limit kills the turn but not the session, so SessionEnd never fires and
# a held lease would squat on in-progress until its TTL runs out — and the wake
# the watcher spent on the dead turn would be lost (the queue snapshot no longer
# changes). StopFailure is the one event that DOES fire there, so this hook:
#   1. stamps the wake-retry flag ($KRAKEN_STATE_DIR/wake-retry); `kraken.py
#      watch` re-emits its wake when the flag is newer than its last emission, so
#      the drain resumes on its own once the limit window resets;
#   2. releases every open claim on this machine (reason "usage limit"): the task
#      requeues in seconds, and this worker re-claims it after reset via the
#      retry wake and continues on the existing branch.
#
# Both ride ONE `kraken.py release-open --stamp-wake-retry`: the program stamps
# before it releases, so an ordering that used to be two bash lines — and could
# be interrupted between them — is now internal to a single process. Even with no
# claim held the consumed wake must be retried, and if the release fails the
# retry still has to happen, which is why the stamp is never conditional on it.
#
# Matcher is `rate_limit` only: a usage limit is account-wide, so any session's
# rate_limit failure implies the worker sessions here are limited too — releasing
# their claims is correct. Per-session error types could fire on a healthy worker
# and must not release its live claim, so they are deliberately not matched.
#
# Best-effort: ALWAYS exits 0. gh still works at limit time, so the release
# normally lands; if not, the lease expiry is the backstop — this hook makes
# recovery immediate, it is not what makes it happen. The StopFailure JSON on
# stdin is unused — the matcher already scoped the error type.
set -u

KRAKEN_ROOT="${CLAUDE_PLUGIN_ROOT:-"$(cd "$(dirname "$0")/.." && pwd)"}"

python3 "$KRAKEN_ROOT/skills/unleash/kraken.py" release-open \
  --stamp-wake-retry --reason "usage limit" >/dev/null 2>&1 || true

exit 0
