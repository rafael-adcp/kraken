#!/usr/bin/env bash
# Claim-window arbitration: claim markers older than the most recent
# released / stale-claim / needs-decision reset marker are ignored —
# otherwise a task once claimed by a dead worker could never be claimed again.
. "$ROOT/tests/lib.sh"

# Reaper path: dead worker claimed, reaper posted a stale-claim marker, human requeued.
mk_issue 7 "reaped task" kraken-task "project:app"
mk_comment 7 '<!-- kraken {"type":"claim","worker":"dead-worker"} -->'
mk_comment 7 '<!-- kraken {"type":"stale-claim","reason":"no activity for 7h"} -->'

out="$(python3 "$SCRIPTS/kraken.py" claim OWNER/tasks 7 w2)"
assert_rc $? 0 "claim after stale-claim reset"
assert_eq "$out" "claim: claimed issue=7 worker=w2" "w2 wins the fresh window"

# Release path: a worker handed the task back with a released marker.
mk_issue 8 "released task" kraken-task "project:app"
mk_comment 8 '<!-- kraken {"type":"claim","worker":"tired-worker"} -->'
mk_comment 8 '<!-- kraken {"type":"released","worker":"tired-worker"} -->'

out="$(python3 "$SCRIPTS/kraken.py" claim OWNER/tasks 8 w3)"
assert_rc $? 0 "claim after released reset"
assert_eq "$out" "claim: claimed issue=8 worker=w3" "w3 wins the fresh window"

# Control: NO reset marker — the old claim still wins, the newcomer loses.
mk_issue 9 "still claimed (label lost out of band)" kraken-task "project:app"
mk_comment 9 '<!-- kraken {"type":"claim","worker":"rightful-owner"} -->'

out="$(python3 "$SCRIPTS/kraken.py" claim OWNER/tasks 9 w4)"
assert_rc $? 10 "claim against a live window loses"
assert_eq "$out" "claim: lost-tiebreaker issue=9 winner=rightful-owner" "rightful owner keeps the task"
