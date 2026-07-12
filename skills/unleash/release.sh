#!/usr/bin/env bash
# release.sh OWNER/tasks ISSUE WORKER_NAME [REASON]
#
# The honest-release transition: in-progress -> queued, for a claim this worker
# is abandoning without delivering (environment can't host the task, subagent
# died, backing out). Never release by just removing the label: the released:
# machine line is what closes the claim window, so stale claimed-by: comments
# can never win a future arbitration.
#
# Order matters: comment FIRST (window closed), then remove the label. If the
# label removal fails, the task stays visibly held — annoying but never
# corrupt; re-run to finish.
#
# Exit codes: 0 released; 20 gh/network failure (re-check state, re-run).
# stdout: one machine-readable line mirroring the exit code.
set -u

REPO="${1:?usage: release.sh OWNER/tasks ISSUE WORKER_NAME [REASON]}"
ISSUE="${2:?usage: release.sh OWNER/tasks ISSUE WORKER_NAME [REASON]}"
WORKER="${3:?usage: release.sh OWNER/tasks ISSUE WORKER_NAME [REASON]}"
REASON="${4:-}"

body="> 🐙 **Kraken worker \`${WORKER}\`** — automated comment from a Claude Code tentacle, not a human.

released: ${WORKER}"
if [ -n "$REASON" ]; then
  body="${body}
reason: ${REASON}"
fi

gh -R "$REPO" issue comment "$ISSUE" --body "$body" >/dev/null \
  || { echo "release: gh-failure issue=${ISSUE} stage=comment"; exit 20; }
gh -R "$REPO" issue edit "$ISSUE" --remove-label in-progress >/dev/null \
  || { echo "release: gh-failure issue=${ISSUE} stage=label"; exit 20; }

echo "release: released issue=${ISSUE} worker=${WORKER}"
exit 0
