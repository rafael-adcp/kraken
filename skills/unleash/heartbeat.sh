#!/usr/bin/env bash
# heartbeat.sh OWNER/tasks ISSUE WORKER_NAME MESSAGE
#
# The liveness signal on a long task: a progress comment that keeps the
# coordination repo's reaper away (it moves silent in-progress issues to
# needs-decision after 6h). Post at least every ~2 hours while executing.
#
# No label changes, and the heartbeat: line is deliberately NOT a claim-window
# reset (see claim.sh) — heartbeating must never make your own claim
# re-claimable.
#
# Exit codes: 0 posted; 20 gh/network failure (safe to just retry — the worst
# outcome of a duplicate heartbeat is a duplicate heartbeat).
# stdout: one machine-readable line mirroring the exit code.
set -u

REPO="${1:?usage: heartbeat.sh OWNER/tasks ISSUE WORKER_NAME MESSAGE}"
ISSUE="${2:?usage: heartbeat.sh OWNER/tasks ISSUE WORKER_NAME MESSAGE}"
WORKER="${3:?usage: heartbeat.sh OWNER/tasks ISSUE WORKER_NAME MESSAGE}"
MESSAGE="${4:?usage: heartbeat.sh OWNER/tasks ISSUE WORKER_NAME MESSAGE}"

gh -R "$REPO" issue comment "$ISSUE" --body "> 🐙 **Kraken worker \`${WORKER}\`** — automated comment from a Claude Code tentacle, not a human.

heartbeat: ${WORKER}

${MESSAGE}" >/dev/null \
  || { echo "heartbeat: gh-failure issue=${ISSUE}"; exit 20; }

echo "heartbeat: posted issue=${ISSUE} worker=${WORKER}"
exit 0
