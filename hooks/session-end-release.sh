#!/usr/bin/env bash
# session-end-release.sh — the bundled SessionEnd hook (hooks.json).
#
# When a worker's session ends *gracefully* (terminal closed, /exit) while it
# still holds a lease, this releases it via the shared loop in
# lib-release-claims.sh, so the task returns to the queue in seconds instead of
# at the end of the lease TTL.
#
# This is an OPTIMIZATION, never the recovery mechanism: under kraken-protocol/9 the
# claim is a lease that expires on its own (PROTOCOL.md §5), so a session that
# fires no hook at all — a hard kill, a crash, a harness with no hook events —
# still frees its task within one TTL. Releasing early is simply more polite to
# the next worker. Graceful end only: a usage limit does NOT end the session, so
# SessionEnd never fires there — that path is the StopFailure hook's
# (stop-failure-release.sh). See the #60 FAQ in README.md.
#
# Both harnesses run it: Claude Code, and GitHub Copilot CLI, which loads this
# plugin's hooks.json and fires SessionEnd with the same Claude-shaped payload
# (verified on Copilot CLI 1.0.88).
#
# Scope: ONLY the ending session's own claims (#173). SessionEnd fires for
# every agent session on the host — a 30-second `claude -p` or `copilot -p`
# opened to read a file included — so the hook matches the event's
# `session_id` against the session `kraken.py claim` recorded. A claim naming
# no session (a bare shell) or an event naming none releases nothing: the lease
# TTL, and the Copilot loop's own release-on-exit, cover those.
#
# Best-effort: ALWAYS exits 0 (a failed release just waits out the TTL).
set -u

. "$(dirname "$0")/lib-release-claims.sh"

release_session_claims "session ended" "$(hook_event_session_id)"

exit 0
