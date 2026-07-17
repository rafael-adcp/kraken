#!/usr/bin/env bash
# Queue-entry quality gate conformance: proves validate-task.yml's shipped run:
# block by extracting and running it verbatim against the gh-stub (mirrors
# tests/t/18-requeue-on-reply.sh). A new kraken-task issue missing its
# project:<name> label, or an empty/absent Goal or Acceptance section, gets ONE
# actionable comment; a compliant task gets none; an edit-after-fix neither
# re-flags a fixed task nor piles up duplicates.
. "$ROOT/tests/lib.sh"

WF="$ROOT/skills/unleash/validate-task.yml"

extract_run() {
  awk '
    /^[[:space:]]*run: \|[[:space:]]*$/ {
      match($0, /^[[:space:]]*/); key = RLENGTH; grab = 1; next
    }
    grab {
      if ($0 ~ /[^[:space:]]/) {
        match($0, /^[[:space:]]*/)
        if (RLENGTH <= key) { grab = 0; next }
      }
      print
    }
  ' "$1" | sed 's/^          //' | tr -d '\r'
}

RUN="$STATE/validate.sh"
extract_run "$WF" > "$RUN"
[ -s "$RUN" ] || fail "could not extract the run block from $WF"

export REPO="OWNER/tasks"

# The compliant issue-form body the bundled task-template.yml produces.
GOOD_BODY="$(printf '### Goal\n\nEndpoint /v2/things returns cursor-paginated results.\n\n### Acceptance\n\n`npm test -- things.spec` passes.\n\n### Notes\n\n_No response_')"

run_case() { export NUM="$1" BODY="$2"; bash "$RUN"; }

# --- #1: missing project label -> one comment naming the missing label -------
mk_issue 1 "compliant body but no project label" kraken-task
mk_body 1 "$GOOD_BODY"
run_case 1 "$GOOD_BODY"
assert_rc $? 0 "#1 run"
assert_eq "$(comment_count 1)" "1" "#1 a missing project label must post exactly one comment"
last_comment 1 | grep -q 'project:<name>' || fail "#1 comment does not name the missing project label"
last_comment 1 | grep -qF '<!-- kraken {"type":"validation"} -->' || fail "#1 comment missing the validation marker"

# --- #2: missing Acceptance section -> one comment naming Acceptance ----------
mk_issue 2 "has project + Goal but empty Acceptance" kraken-task "project:app"
BODY2="$(printf '### Goal\n\nShip the thing.\n\n### Acceptance\n\n_No response_\n\n### Notes\n\n_No response_')"
mk_body 2 "$BODY2"
run_case 2 "$BODY2"
assert_rc $? 0 "#2 run"
assert_eq "$(comment_count 2)" "1" "#2 an empty Acceptance must post exactly one comment"
last_comment 2 | grep -q 'Acceptance' || fail "#2 comment does not name the missing Acceptance section"
last_comment 2 | grep -q 'project:<name>' && fail "#2 wrongly flagged the present project label"

# --- #2b: hand-written issue with no headings at all -> Goal+Acceptance flagged
mk_issue 20 "hand-written, no issue-form headings" kraken-task "project:app"
BODY20="just do the thing, you know what I mean"
mk_body 20 "$BODY20"
run_case 20 "$BODY20"
assert_rc $? 0 "#2b run"
assert_eq "$(comment_count 20)" "1" "#2b a heading-less body must post exactly one comment"
last_comment 20 | grep -q 'Goal'       || fail "#2b comment does not name the missing Goal"
last_comment 20 | grep -q 'Acceptance' || fail "#2b comment does not name the missing Acceptance"

# --- #3: compliant task -> no comment (no noise on the happy path) ------------
mk_issue 3 "fully compliant task" kraken-task "project:app"
mk_body 3 "$GOOD_BODY"
run_case 3 "$GOOD_BODY"
assert_rc $? 0 "#3 run"
assert_eq "$(comment_count 3)" "0" "#3 a compliant task must get no comment"

# --- #4: non-kraken-task issue -> no-op --------------------------------------
mk_issue 4 "not a kraken task" "project:app"
mk_body 4 "whatever"
run_case 4 "whatever"
assert_rc $? 0 "#4 run"
assert_eq "$(comment_count 4)" "0" "#4 a non-kraken-task issue must be a no-op"

# --- #5: debounce -> a re-run with the same missing set adds no duplicate -----
mk_issue 5 "missing label, edited twice" kraken-task
BODY5="$GOOD_BODY"
mk_body 5 "$BODY5"
run_case 5 "$BODY5"
assert_rc $? 0 "#5 first run"
assert_eq "$(comment_count 5)" "1" "#5 first run posts one comment"
# Simulate an edit that does not change what's missing (still no project label).
run_case 5 "$BODY5"
assert_rc $? 0 "#5 second run"
assert_eq "$(comment_count 5)" "1" "#5 an identical re-flag must not post a duplicate"

# --- #6: edit-after-fix -> fixing the task stops the flag path, no re-flag ----
# The operator adds the project label (the fix); the next run finds the task
# compliant and posts nothing further.
gh issue edit 5 -R "$REPO" --add-label "project:app"
run_case 5 "$BODY5"
assert_rc $? 0 "#6 run after fix"
assert_eq "$(comment_count 5)" "1" "#6 a fixed task must not get a new comment"

exit 0
