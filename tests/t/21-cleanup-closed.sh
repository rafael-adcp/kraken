#!/usr/bin/env bash
# Cleanup conformance (§10): proves cleanup-closed.yml's shipped run: block by
# extracting and running it verbatim against the gh-stub (mirrors tests/t/18).
# On a closed kraken-task issue every label MUST be stripped except kraken-task
# itself and its project:<name> label, so closed issues read clean and
# label-based queue filters never match dead state.
. "$ROOT/tests/lib.sh"

WF="$ROOT/skills/unleash/cleanup-closed.yml"

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

RUN="$STATE/cleanup.sh"
extract_run "$WF" > "$RUN"
[ -s "$RUN" ] || fail "could not extract the run block from $WF"

export REPO="OWNER/tasks"
run_case() { export NUM="$1"; bash "$RUN"; }

# A stale in-progress claim left on a closed issue: every state label goes,
# kraken-task and project:app stay.
mk_issue 1 "closed with a stale held label" kraken-task "project:app" in-progress
echo "closed" > "$STATE/issues/1/state"
run_case 1
assert_rc $? 0 "#1 run"
has_label 1 kraken-task   || fail "#1 kraken-task wrongly stripped"
has_label 1 "project:app" || fail "#1 project:app wrongly stripped"
has_label 1 in-progress   && fail "#1 in-progress not stripped from a closed issue"

# Multiple dead-state labels plus an unrelated one: all non-identity labels go.
mk_issue 2 "closed with several labels" kraken-task "project:web" awaiting-merge needs-decision priority:high
echo "closed" > "$STATE/issues/2/state"
run_case 2
assert_rc $? 0 "#2 run"
has_label 2 kraken-task    || fail "#2 kraken-task wrongly stripped"
has_label 2 "project:web"  || fail "#2 project:web wrongly stripped"
has_label 2 awaiting-merge && fail "#2 awaiting-merge not stripped"
has_label 2 needs-decision && fail "#2 needs-decision not stripped"
has_label 2 priority:high  && fail "#2 non-kraken label not stripped"

# Already clean: nothing but identity labels — a no-op, no error.
mk_issue 3 "closed and already clean" kraken-task "project:app"
echo "closed" > "$STATE/issues/3/state"
run_case 3
assert_rc $? 0 "#3 run"
has_label 3 kraken-task   || fail "#3 kraken-task wrongly stripped"
has_label 3 "project:app" || fail "#3 project:app wrongly stripped"

exit 0
