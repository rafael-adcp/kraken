#!/usr/bin/env bash
# escalate.sh OWNER/tasks ISSUE WORKER_NAME QUESTION_FILE
#
# The in-progress -> needs-decision transition: the task is blocked on a human
# decision. QUESTION_FILE holds the agent-composed body — the question, the
# options, and a recommendation (that part stays the LLM's job; this script
# only executes the dance).
#
# Order matters: the needs-decision: machine line is posted FIRST — it closes
# the claim window, so when the human answers and removes the label, ANY worker
# can win the fresh arbitration (not just the one that escalated). Then the
# labels swap in one call. If the swap fails the task stays in-progress —
# still held, never corrupt; re-run to finish (a duplicate comment is
# harmless).
#
# Exit codes: 0 escalated; 2 bad invocation; 20 gh/network failure.
# stdout: one machine-readable line mirroring the exit code.
set -u

REPO="${1:?usage: escalate.sh OWNER/tasks ISSUE WORKER_NAME QUESTION_FILE}"
ISSUE="${2:?usage: escalate.sh OWNER/tasks ISSUE WORKER_NAME QUESTION_FILE}"
WORKER="${3:?usage: escalate.sh OWNER/tasks ISSUE WORKER_NAME QUESTION_FILE}"
QUESTION_FILE="${4:?usage: escalate.sh OWNER/tasks ISSUE WORKER_NAME QUESTION_FILE}"

[ -f "$QUESTION_FILE" ] || { echo "escalate: no such file ${QUESTION_FILE}" >&2; exit 2; }

gh -R "$REPO" issue comment "$ISSUE" --body "> 🐙 **Kraken worker \`${WORKER}\`** — automated comment from a Claude Code tentacle, not a human.

needs-decision: ${WORKER}

$(cat "$QUESTION_FILE")" >/dev/null \
  || { echo "escalate: gh-failure issue=${ISSUE} stage=comment"; exit 20; }

gh -R "$REPO" issue edit "$ISSUE" --remove-label in-progress --add-label needs-decision >/dev/null \
  || { echo "escalate: gh-failure issue=${ISSUE} stage=labels"; exit 20; }

echo "escalate: escalated issue=${ISSUE} worker=${WORKER}"
exit 0
