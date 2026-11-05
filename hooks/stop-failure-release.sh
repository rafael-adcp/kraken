#!/usr/bin/env bash
# stop-failure-release.sh — the bundled StopFailure hook (hooks.json, matcher
# `rate_limit`).
#
# A usage limit kills the turn but not the session, so SessionEnd never fires and
# a held lease would squat on in-progress until its TTL runs out. StopFailure is
# the one event that DOES fire there, so this hook releases every open claim on
# this machine (reason "usage limit"): the task requeues in seconds, and the next
# drain — a fresh `kraken-loop.sh` pass once the limit window resets — re-claims
# it and continues on the existing branch.
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
# Under `claude -p` the process exits on the failed turn and signals its hooks
# ~15ms after starting them (verified on Claude Code 2.1.283): ignore that, so
# the hook lives long enough to hand the release off.
trap '' HUP INT TERM

. "$(dirname "$0")/lib-release-claims.sh"

# Detached, like SessionEnd's: under `claude -p` the process exits on the failed
# turn and would kill a release still in flight (see release_detached).
release_detached release_all_claims "usage limit"

exit 0
