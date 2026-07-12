#!/usr/bin/env bash
# claim.sh OWNER/tasks ISSUE WORKER_NAME
#
# The queued -> in-progress transition, exactly as unleash protocol step 3
# specifies, executed identically every time:
#   1. guard     — re-fetch the labels; already held -> exit 11, zero writes.
#                  Never stack in-progress on a held task (in-progress +
#                  awaiting-merge is exactly the corruption the reaper mops up).
#   2. label     — add in-progress
#   3. comment   — attribution disclaimer + "claimed-by: <worker>"
#   4. arbitrate — re-read the comments; the FIRST claimed-by: of the current
#                  claim window wins (server-side comment ordering is the
#                  tiebreaker — every worker authenticates as the same user, so
#                  assignees can't arbitrate). The window starts after the most
#                  recent released: / stale-claim: / needs-decision: machine
#                  line, so a dead worker's old claim never blocks re-claiming.
#
# Exit codes (the agent branches on these):
#    0  claimed — this worker owns the task
#   10  lost the tiebreaker — back off (remove nothing), pick the next candidate
#   11  no longer clear — a held label appeared since listing; skip the task
#   20  gh/network failure — claim state unknown; re-check before retrying
# stdout: one machine-readable line mirroring the exit code.
set -u

REPO="${1:?usage: claim.sh OWNER/tasks ISSUE WORKER_NAME}"
ISSUE="${2:?usage: claim.sh OWNER/tasks ISSUE WORKER_NAME}"
WORKER="${3:?usage: claim.sh OWNER/tasks ISSUE WORKER_NAME}"

# 1. Guard.
labels="$(gh -R "$REPO" issue view "$ISSUE" --json labels \
  --jq '[.labels[].name] | join(" ")')" \
  || { echo "claim: gh-failure issue=${ISSUE} stage=guard"; exit 20; }
labels="${labels//$'\r'/}"
for held in in-progress needs-decision awaiting-merge; do
  case " $labels " in
    *" $held "*) echo "claim: held issue=${ISSUE} label=${held}"; exit 11 ;;
  esac
done

# 2. Label, then 3. immediately the claim comment (disclaimer above the
# machine line, blank line between, or GitHub folds the body into the quote).
gh -R "$REPO" issue edit "$ISSUE" --add-label in-progress >/dev/null \
  || { echo "claim: gh-failure issue=${ISSUE} stage=label"; exit 20; }
gh -R "$REPO" issue comment "$ISSUE" --body "> 🐙 **Kraken worker \`${WORKER}\`** — automated comment from a Claude Code tentacle, not a human.

claimed-by: ${WORKER}" >/dev/null \
  || { echo "claim: gh-failure issue=${ISSUE} stage=comment"; exit 20; }

# 4. Arbitrate. Machine lines are one per line, so scanning the concatenated
# comment bodies in server order is enough — no per-comment boundaries needed.
stream="$(gh -R "$REPO" issue view "$ISSUE" --json comments \
  --jq '.comments[].body')" \
  || { echo "claim: gh-failure issue=${ISSUE} stage=arbitrate"; exit 20; }

winner=""
while IFS= read -r line; do
  line="${line%$'\r'}"
  case "$line" in
    released:* | stale-claim:* | needs-decision:*)
      winner="" # claim-window reset: older claimed-by lines no longer count
      ;;
    claimed-by:*)
      if [ -z "$winner" ]; then
        winner="${line#claimed-by:}"
        winner="${winner# }"
      fi
      ;;
  esac
done <<EOF
$stream
EOF

if [ "$winner" = "$WORKER" ]; then
  echo "claim: claimed issue=${ISSUE} worker=${WORKER}"
  exit 0
fi
echo "claim: lost-tiebreaker issue=${ISSUE} winner=${winner:-unknown}"
exit 10
