#!/usr/bin/env bash
# list-startable.sh owns the blocked-by check too: a candidate with an open
# native blocker (or, as a fallback, an open `depends-on: #N` target) must
# never list as startable in either mode — closing the blocker un-blocks it.
. "$ROOT/tests/lib.sh"

# --- native blocked-by: open blocker excludes; closing it un-excludes -------
mk_issue 1 "blocker open"      kraken-task "project:app"
mk_issue 2 "blocked candidate" kraken-task "project:app"
mk_blocked_by 2 1

out="$(bash "$SCRIPTS/list-startable.sh" OWNER/tasks app)"
assert_rc $? 0 "default mode exit (blocker open)"
expected="$(printf '1\tblocker open')"
assert_eq "$out" "$expected" "blocked candidate excluded while blocker open"

out="$(bash "$SCRIPTS/list-startable.sh" OWNER/tasks app --snapshot)"
assert_rc $? 0 "snapshot mode exit (blocker open)"
expected="$(printf '1:startable\n2:held')"
assert_eq "$out" "$expected" "blocked candidate reports held in snapshot"

# Close the blocker: the candidate now lists, oldest-first ordering intact.
echo "closed" > "$STATE/issues/1/state"

out="$(bash "$SCRIPTS/list-startable.sh" OWNER/tasks app)"
assert_rc $? 0 "default mode exit (blocker closed)"
expected="$(printf '2\tblocked candidate')"
assert_eq "$out" "$expected" "candidate lists once blocker closes (blocker itself is closed, so absent)"

out="$(bash "$SCRIPTS/list-startable.sh" OWNER/tasks app --snapshot)"
assert_rc $? 0 "snapshot mode exit (blocker closed)"
expected="$(printf '2:startable')"
assert_eq "$out" "$expected" "candidate reports startable once blocker closes (blocker is closed -> not in open-issue snapshot)"

# --- depends-on: #N body fallback, honored only with no native blockers ----
mk_issue 3 "dep target open" kraken-task "project:app"
mk_issue 4 "fallback candidate" kraken-task "project:app"
mk_body 4 "goal text

depends-on: #3
"

out="$(bash "$SCRIPTS/list-startable.sh" OWNER/tasks app)"
assert_rc $? 0 "default mode exit (depends-on open)"
expected="$(printf '2\tblocked candidate\n3\tdep target open')"
assert_eq "$out" "$expected" "depends-on candidate excluded while target open"

out="$(bash "$SCRIPTS/list-startable.sh" OWNER/tasks app --snapshot)"
assert_rc $? 0 "snapshot mode exit (depends-on open)"
expected="$(printf '2:startable\n3:startable\n4:held')"
assert_eq "$out" "$expected" "depends-on candidate reports held while target open"

# Close the depends-on target: the fallback candidate now lists.
echo "closed" > "$STATE/issues/3/state"

out="$(bash "$SCRIPTS/list-startable.sh" OWNER/tasks app)"
assert_rc $? 0 "default mode exit (depends-on closed)"
expected="$(printf '2\tblocked candidate\n4\tfallback candidate')"
assert_eq "$out" "$expected" "depends-on candidate lists once target closes"

out="$(bash "$SCRIPTS/list-startable.sh" OWNER/tasks app --snapshot)"
assert_rc $? 0 "snapshot mode exit (depends-on closed)"
expected="$(printf '2:startable\n4:startable')"
assert_eq "$out" "$expected" "depends-on candidate reports startable once target closes"

# --- native blockers take priority over an (irrelevant) depends-on line ----
mk_issue 5 "native blocker still open" kraken-task "project:app"
mk_issue 6 "has both"                  kraken-task "project:app"
mk_blocked_by 6 5
mk_body 6 "depends-on: #3" # #3 is closed — if this fallback were consulted it would wrongly clear #6

out="$(bash "$SCRIPTS/list-startable.sh" OWNER/tasks app --snapshot)"
assert_rc $? 0 "snapshot mode exit (native + depends-on)"
printf '%s\n' "$out" | grep -qxF "6:held" || fail "native blocker must win over an irrelevant depends-on fallback"
