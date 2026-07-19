#!/usr/bin/env bash
# session-end-release.sh — the bundled SessionEnd hook (hooks.json).
#
# When a worker's session ends *gracefully* (terminal closed, /exit) while it
# still holds an open claim, this releases it via the shared loop in
# lib-release-claims.sh, so the task returns to the queue in seconds instead of
# waiting ~6h for the reaper.
#
# Graceful end only: a usage limit does NOT end the session, so SessionEnd never
# fires there — that path is the StopFailure hook's (stop-failure-release.sh). A
# hard kill / crash fires neither hook; the reaper stays the backstop. See the
# #60 FAQ in README.md.
#
# Best-effort: ALWAYS exits 0 (a failed release falls back to the reaper); the
# SessionEnd JSON on stdin is unused.
set -u

. "$(dirname "$0")/lib-release-claims.sh"

release_all_claims "session ended"

exit 0
