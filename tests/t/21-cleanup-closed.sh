#!/usr/bin/env bash
# Cleanup conformance (§10): on a closed kraken-task issue every label MUST be
# stripped except kraken-task itself and its project:<name> label, so closed
# issues read clean and label-based queue filters never match dead state.
#
# cleanup-closed.yml is now a thin exec of `kraken.py cleanup` (issue #37/#39),
# so this drives the shipped subcommand directly against the gh-stub — the same
# way the suite exercises every other transition (tests/t/17, /18, /25) —
# preserving the scenarios the old extract-and-run test pinned.
. "$ROOT/tests/lib.sh"

export REPO="OWNER/tasks"

# A stale in-progress claim left on a closed issue: every state label goes,
# kraken-task and project:app stay.
mk_issue 1 "closed with a stale held label" kraken-task "project:app" in-progress
echo "closed" > "$STATE/issues/1/state"
python3 "$SCRIPTS/kraken.py" cleanup "$REPO" 1
assert_rc $? 0 "#1 run"
has_label 1 kraken-task   || fail "#1 kraken-task wrongly stripped"
has_label 1 "project:app" || fail "#1 project:app wrongly stripped"
has_label 1 in-progress   && fail "#1 in-progress not stripped from a closed issue"

# Multiple dead-state labels plus an unrelated one: all non-identity labels go.
mk_issue 2 "closed with several labels" kraken-task "project:web" awaiting-merge needs-decision priority:high
echo "closed" > "$STATE/issues/2/state"
python3 "$SCRIPTS/kraken.py" cleanup "$REPO" 2
assert_rc $? 0 "#2 run"
has_label 2 kraken-task    || fail "#2 kraken-task wrongly stripped"
has_label 2 "project:web"  || fail "#2 project:web wrongly stripped"
has_label 2 awaiting-merge && fail "#2 awaiting-merge not stripped"
has_label 2 needs-decision && fail "#2 needs-decision not stripped"
has_label 2 priority:high  && fail "#2 non-kraken label not stripped"

# Already clean: nothing but identity labels — a no-op, no error.
mk_issue 3 "closed and already clean" kraken-task "project:app"
echo "closed" > "$STATE/issues/3/state"
python3 "$SCRIPTS/kraken.py" cleanup "$REPO" 3
assert_rc $? 0 "#3 run"
has_label 3 kraken-task   || fail "#3 kraken-task wrongly stripped"
has_label 3 "project:app" || fail "#3 project:app wrongly stripped"

# Not a kraken-task issue: a no-op guard (the workflow's if: gate, re-checked by
# kraken.py), nothing stripped even though a state label is present.
mk_issue 4 "closed non-task issue" needs-decision priority:high
echo "closed" > "$STATE/issues/4/state"
python3 "$SCRIPTS/kraken.py" cleanup "$REPO" 4
assert_rc $? 0 "#4 run"
has_label 4 needs-decision || fail "#4 label wrongly stripped from a non-task issue"
has_label 4 priority:high  || fail "#4 label wrongly stripped from a non-task issue"

exit 0
