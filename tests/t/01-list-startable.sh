#!/usr/bin/env bash
# list-startable.sh: startable filter, oldest-first ordering, snapshot mode.
. "$ROOT/tests/lib.sh"

mk_issue 1 "old startable"  kraken-task "project:app"
mk_issue 2 "claimed"        kraken-task "project:app" in-progress
mk_issue 3 "other project"  kraken-task "project:other"
mk_issue 4 "young startable" kraken-task "project:app"
mk_issue 5 "delivered"      kraken-task "project:app" awaiting-merge
mk_issue 6 "escalated"      kraken-task "project:app" needs-decision
mk_issue 7 "closed" kraken-task "project:app"
echo "closed" > "$STATE/issues/7/state"

# Default mode: startable only, oldest first, number<TAB>title.
out="$(bash "$SCRIPTS/list-startable.sh" OWNER/tasks app)"
assert_rc $? 0 "default mode exit"
expected="$(printf '1\told startable\n4\tyoung startable')"
assert_eq "$out" "$expected" "default mode output"

# Snapshot mode: every open task in the project, sorted by number, with state.
out="$(bash "$SCRIPTS/list-startable.sh" OWNER/tasks app --snapshot)"
assert_rc $? 0 "snapshot mode exit"
expected="$(printf '1:startable\n2:held\n4:startable\n5:held\n6:held')"
assert_eq "$out" "$expected" "snapshot mode output"

# Empty queue: exit 0, no output.
out="$(bash "$SCRIPTS/list-startable.sh" OWNER/tasks nothing-here)"
assert_rc $? 0 "empty queue exit"
assert_eq "$out" "" "empty queue output"
