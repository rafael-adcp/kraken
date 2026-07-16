#!/usr/bin/env bash
# The claim state file lifecycle: kraken.py claim writes
# ${KRAKEN_STATE_DIR}/claim-<worker>.json on a won claim (exit 0), and every
# terminal worker transition — deliver, escalate, release — removes it. This is
# the primitive the SessionEnd auto-release hook reads to know a claim is open.
. "$ROOT/tests/lib.sh"

# Redirect the claim state dir into this test's scratch so we never touch $HOME.
export KRAKEN_STATE_DIR="$STATE/kraken"
state_file="$KRAKEN_STATE_DIR/claim-w1.json"

# --- claim writes the state file on exit 0 ----------------------------------
mk_issue 7 "a task" kraken-task "project:app"
out="$(python3 "$SCRIPTS/kraken.py" claim OWNER/tasks 7 w1)"
assert_rc $? 0 "clean claim exit"
[ -f "$state_file" ] || fail "claim did not write the state file"
# Contents: repo, issue, worker all recorded (the hook reads them back).
grep -q '"repo"' "$state_file"   || fail "state file missing repo field"
grep -q '"issue"' "$state_file"  || fail "state file missing issue field"
grep -q '"worker"' "$state_file" || fail "state file missing worker field"
grep -q 'OWNER/tasks' "$state_file" || fail "state file did not record the repo"
grep -q 'w1'         "$state_file" || fail "state file did not record the worker"

# --- a lost/held claim writes NO state file ---------------------------------
mk_issue 8 "held task" kraken-task "project:app" in-progress
out="$(python3 "$SCRIPTS/kraken.py" claim OWNER/tasks 8 w1)"
assert_rc $? 11 "held claim exit"
[ -f "$KRAKEN_STATE_DIR/claim-w1.json" ] && [ ! -f "$state_file" ] \
  && fail "impossible: file check confusion" # keep w1's real file from issue 7 intact
# (issue 7's file must still exist — issue 8's guard must not have removed it)
[ -f "$state_file" ] || fail "guard/skip wrongly removed an unrelated claim state file"

# --- release removes it ------------------------------------------------------
out="$(python3 "$SCRIPTS/kraken.py" release OWNER/tasks 7 w1 "backing out")"
assert_rc $? 0 "release exit"
[ -f "$state_file" ] && fail "release did not remove the state file"

# --- escalate removes it -----------------------------------------------------
mk_issue 9 "blocked task" kraken-task "project:app"
python3 "$SCRIPTS/kraken.py" claim OWNER/tasks 9 w1 >/dev/null
esf="$KRAKEN_STATE_DIR/claim-w1.json"
[ -f "$esf" ] || fail "re-claim for escalate test did not write state file"
q="$STATE/q.md"; echo "which way?" > "$q"
out="$(python3 "$SCRIPTS/kraken.py" escalate OWNER/tasks 9 w1 "$q")"
assert_rc $? 0 "escalate exit"
[ -f "$esf" ] && fail "escalate did not remove the state file"

# --- deliver removes it ------------------------------------------------------
mk_issue 10 "shipped task" kraken-task "project:app"
python3 "$SCRIPTS/kraken.py" claim OWNER/tasks 10 w1 >/dev/null
dsf="$KRAKEN_STATE_DIR/claim-w1.json"
[ -f "$dsf" ] || fail "re-claim for deliver test did not write state file"
r="$STATE/r.md"; echo "done, validated" > "$r"
out="$(python3 "$SCRIPTS/kraken.py" deliver OWNER/tasks 10 w1 "$r" "https://x/pr/1")"
assert_rc $? 0 "deliver exit"
[ -f "$dsf" ] && fail "deliver did not remove the state file"

exit 0
