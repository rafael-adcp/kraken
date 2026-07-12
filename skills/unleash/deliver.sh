#!/usr/bin/env bash
# deliver.sh OWNER/tasks ISSUE WORKER_NAME RESULT_FILE [PR_URL]
#
# The in-progress -> awaiting-merge transition: the work is delivered for
# review. RESULT_FILE holds the agent-composed result comment — what was done,
# how it was validated against the issue's acceptance, links (that part stays
# the LLM's job; this script only executes the dance). PR_URL is the draft
# PR/MR when there is one — omitted only when the work repo can't take one
# (diff-in-comment path).
#
# The delivered: machine line closes the claim window: when a review bounces
# the task back (human comments feedback and removes awaiting-merge), ANY
# worker can win the fresh arbitration and continue on the existing branch.
#
# Order matters: comment first, then the label swap in one call. If the swap
# fails the task stays in-progress with its result recorded — the reaper may
# eventually flag it, which is triage, not corruption; re-run to finish.
#
# Exit codes: 0 delivered; 2 bad invocation; 20 gh/network failure.
# stdout: one machine-readable line mirroring the exit code.
set -u

REPO="${1:?usage: deliver.sh OWNER/tasks ISSUE WORKER_NAME RESULT_FILE [PR_URL]}"
ISSUE="${2:?usage: deliver.sh OWNER/tasks ISSUE WORKER_NAME RESULT_FILE [PR_URL]}"
WORKER="${3:?usage: deliver.sh OWNER/tasks ISSUE WORKER_NAME RESULT_FILE [PR_URL]}"
RESULT_FILE="${4:?usage: deliver.sh OWNER/tasks ISSUE WORKER_NAME RESULT_FILE [PR_URL]}"
PR_URL="${5:-}"

[ -f "$RESULT_FILE" ] || { echo "deliver: no such file ${RESULT_FILE}" >&2; exit 2; }

machine="delivered: ${WORKER}"
if [ -n "$PR_URL" ]; then
  machine="${machine}
pr: ${PR_URL}"
fi

gh -R "$REPO" issue comment "$ISSUE" --body "> 🐙 **Kraken worker \`${WORKER}\`** — automated comment from a Claude Code tentacle, not a human.

${machine}

$(cat "$RESULT_FILE")" >/dev/null \
  || { echo "deliver: gh-failure issue=${ISSUE} stage=comment"; exit 20; }

gh -R "$REPO" issue edit "$ISSUE" --remove-label in-progress --add-label awaiting-merge >/dev/null \
  || { echo "deliver: gh-failure issue=${ISSUE} stage=labels"; exit 20; }

echo "deliver: delivered issue=${ISSUE} worker=${WORKER}${PR_URL:+ pr=${PR_URL}}"
exit 0
