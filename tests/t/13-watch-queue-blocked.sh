#!/usr/bin/env bash
# watch-queue.sh's entire emit gate is `count -gt 0` on
# `list-startable.sh --snapshot`'s ":startable$" lines (see the script: it has
# no filter logic of its own since this task made list-startable.sh the sole
# owner of "startable"). So proving a blocked-only queue's snapshot carries
# zero ":startable" lines IS the no-wake proof — that count is the exact
# expression watch-queue's loop tests every poll. (Driving the loop itself
# needs real backgrounding + signal delivery across nested bash processes,
# which is not reliable in this harness/OS combination — the stub-based
# snapshot check is the deterministic seam.)
. "$ROOT/tests/lib.sh"

# The blocker lives in a different project — it is not itself a project:app
# candidate, so the project:app snapshot below is genuinely blocked-only.
mk_issue 1 "blocker (other project)" kraken-task "project:other"
mk_issue 2 "blocked candidate"       kraken-task "project:app"
mk_blocked_by 2 1

snapshot="$(bash "$SCRIPTS/list-startable.sh" OWNER/tasks app --snapshot)"
assert_rc $? 0 "snapshot exit (blocked-only queue)"
assert_eq "$snapshot" "2:held" "blocked-only queue snapshot has no startable line"

startable_count="$(printf '%s\n' "$snapshot" | grep -c ':startable$')" || true
assert_eq "$startable_count" "0" \
  "blocked-only queue: zero startable lines (watch-queue's exact emit gate — this is what keeps it from waking)"

# Closing the blocker flips the candidate to startable in the snapshot — the
# change watch-queue's loop compares against its previous snapshot and wakes on.
echo "closed" > "$STATE/issues/1/state"
snapshot="$(bash "$SCRIPTS/list-startable.sh" OWNER/tasks app --snapshot)"
assert_rc $? 0 "snapshot exit (blocker closed)"
assert_eq "$snapshot" "2:startable" "closing the blocker flips the candidate to startable"

startable_count="$(printf '%s\n' "$snapshot" | grep -c ':startable$')" || true
assert_eq "$startable_count" "1" "once the blocker closes, the snapshot has exactly the newly-clear task startable"

# watch-queue.sh's own gate is textually verifiable too: it must not carry
# the false-alarm re-emission timer this task removed.
grep -q 'REMIND_SECONDS' "$SCRIPTS/watch-queue.sh" && fail "watch-queue.sh must not retain the 30-min re-emission safety net"
grep -q 'count.*-gt 0.*snapshot.*!=.*prev' "$SCRIPTS/watch-queue.sh" \
  || fail "watch-queue.sh must gate emission on count>0 AND snapshot changed, nothing else"
