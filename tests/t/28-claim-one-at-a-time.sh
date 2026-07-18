#!/usr/bin/env bash
# PROTOCOL.md §5 (L211): a worker MUST work one task at a time and MUST NOT
# claim a second task while it holds a claim. kraken.py claim reads the same
# ${KRAKEN_STATE_DIR}/claim-<worker>.json state file whose lifecycle tests/t/15
# pins, and refuses (exit 11, writing nothing) when it already marks an open
# claim — for `claim` on a *different* issue and for `claim-next` on any open
# claim. A recorded claim on the *same* issue is a permitted re-claim (the §5
# network-failure caveat: re-check the ambiguous claim, don't treat the retry
# as a second task). Once the claim is resolved (deliver / escalate / release
# removes the file), the worker is clear to claim again.
. "$ROOT/tests/lib.sh"

# Redirect the claim state dir into this test's scratch so we never touch $HOME.
export KRAKEN_STATE_DIR="$STATE/kraken"
state_file="$KRAKEN_STATE_DIR/claim-w1.json"

# --- w1 takes its one task --------------------------------------------------
mk_issue 7 "first task"  kraken-task "project:app"
mk_issue 8 "second task" kraken-task "project:app"
out="$(python3 "$SCRIPTS/kraken.py" claim OWNER/tasks 7 w1)"
assert_rc $? 0 "clean first claim exit"
[ -f "$state_file" ] || fail "first claim did not write the state file"
has_label 7 in-progress || fail "first claim did not label issue 7 in-progress"

# --- claim of a DIFFERENT task is refused while the claim is open -----------
before="$(comment_count 8)"
out="$(python3 "$SCRIPTS/kraken.py" claim OWNER/tasks 8 w1)"
assert_rc $? 11 "second claim (different task) is refused"
printf '%s\n' "$out" | grep -q "refused" \
  || fail "refusal message should name the refusal (got: $out)"
printf '%s\n' "$out" | grep -q "holds=7" \
  || fail "refusal should report the open claim it holds (got: $out)"
# Refusal writes NOTHING: no label swap, no comment on the second task.
has_label 8 in-progress && fail "refused claim wrongly labeled issue 8 in-progress"
assert_eq "$(comment_count 8)" "$before" "refused claim wrongly commented on issue 8"
# ...and the held claim's own state file is untouched.
[ -f "$state_file" ] || fail "refused claim wrongly removed the open claim state file"
grep -q '"issue": "7"' "$state_file" || fail "open claim state file no longer records issue 7"

# --- claim-next is refused too while any claim is open ----------------------
out="$(python3 "$SCRIPTS/kraken.py" claim-next OWNER/tasks app w1)"
assert_rc $? 11 "claim-next is refused while a claim is held"
printf '%s\n' "$out" | grep -q "refused" \
  || fail "claim-next refusal should name the refusal (got: $out)"
# It never touched the queue: issue 8 stays startable, uncommented.
has_label 8 in-progress && fail "refused claim-next wrongly labeled issue 8 in-progress"
assert_eq "$(comment_count 8)" "$before" "refused claim-next wrongly commented on issue 8"

# --- re-claiming the SAME issue is allowed (the network-failure caveat) -----
# Simulate an ambiguous first claim being re-checked: the state file still
# records issue 7, but its held label is gone (window reset). The guard must
# NOT treat re-claiming issue 7 as a forbidden second task.
sed -i '/^in-progress$/d' "$STATE/issues/7/labels"
out="$(python3 "$SCRIPTS/kraken.py" claim OWNER/tasks 7 w1)"
assert_rc $? 0 "re-claiming the same held issue is permitted"
printf '%s\n' "$out" | grep -q "refused" \
  && fail "re-claiming the same issue must not be refused as a second claim"

# --- resolving the claim clears the guard -----------------------------------
out="$(python3 "$SCRIPTS/kraken.py" release OWNER/tasks 7 w1 "backing out")"
assert_rc $? 0 "release exit"
[ -f "$state_file" ] && fail "release did not remove the state file"
# With no open claim, the worker is clear to take a new task.
out="$(python3 "$SCRIPTS/kraken.py" claim OWNER/tasks 8 w1)"
assert_rc $? 0 "claim after release is no longer refused"
has_label 8 in-progress || fail "post-release claim did not label issue 8 in-progress"

exit 0
