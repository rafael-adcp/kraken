#!/usr/bin/env bash
# session-end-release.sh — the bundled SessionEnd hook (hooks.json).
#
# When a worker's session ends *gracefully* (terminal closed, /exit) while it
# still holds a lease, this releases it via the shared loop in
# lib-release-claims.sh, so the task returns to the queue in seconds instead of
# at the end of the lease TTL.
#
# This is an OPTIMIZATION, never the recovery mechanism: under protocol/6 the
# claim is a lease that expires on its own (PROTOCOL.md §5), so a session that
# fires no hook at all — a hard kill, a crash, a harness with no hook events —
# still frees its task within one TTL. Releasing early is simply more polite to
# the next worker. Graceful end only: a usage limit does NOT end the session, so
# SessionEnd never fires there — that path is the StopFailure hook's
# (stop-failure-release.sh). See the #60 FAQ in README.md.
#
# Best-effort: ALWAYS exits 0 (a failed release just waits out the TTL); the
# SessionEnd JSON on stdin is unused.
set -u

. "$(dirname "$0")/lib-release-claims.sh"

release_all_claims "session ended"

exit 0
