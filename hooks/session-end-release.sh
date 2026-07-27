#!/usr/bin/env bash
# session-end-release.sh — the bundled SessionEnd hook (hooks.json).
#
# When a worker's session ends *gracefully* (terminal closed, /exit) while it
# still holds a lease, this releases it so the task returns to the queue in
# seconds instead of at the end of the lease TTL.
#
# This is an OPTIMIZATION, never the recovery mechanism: under protocol/7 the
# claim is a lease that expires on its own (PROTOCOL.md §5), so a session that
# fires no hook at all — a hard kill, a crash, a harness with no hook events —
# still frees its task within one TTL. Releasing early is simply more polite to
# the next worker. Graceful end only: a usage limit does NOT end the session, so
# SessionEnd never fires there — that path is the StopFailure hook's
# (stop-failure-release.sh). See the #60 FAQ in README.md.
#
# The whole mechanism is `kraken.py release-open`: discovering the open claims,
# reading their records and releasing them in the §9 order is the program's job.
# This file is the Claude Code event binding and nothing else — it replaced a
# bash library that re-parsed the claim-state JSON with jq-or-sed.
#
# UNSCOPED on purpose: SessionEnd is the whole session going away, so every
# claim it recorded on this machine is ending with it. Callers that share a
# machine with other workers (`kraken.py loop`) pass --worker instead.
#
# Best-effort: ALWAYS exits 0 (a failed release just waits out the TTL); the
# SessionEnd JSON on stdin is unused.
set -u

KRAKEN_ROOT="${CLAUDE_PLUGIN_ROOT:-"$(cd "$(dirname "$0")/.." && pwd)"}"

python3 "$KRAKEN_ROOT/skills/unleash/kraken.py" release-open \
  --reason "session ended" >/dev/null 2>&1 || true

exit 0
