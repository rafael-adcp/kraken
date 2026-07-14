#!/usr/bin/env bash
# Scenario: ambiguous goal.
#
# When a task's goal admits materially different implementations and getting it
# wrong is expensive, the worker MUST escalate rather than guess (PROTOCOL.md §7,
# SKILL.md step a): post the question — options AND a recommendation — with the
# `needs-decision:` machine line, then swap in-progress -> needs-decision.
#
# Assertion surface = artifacts only:
#   - task ends labeled needs-decision, not in-progress/awaiting-merge,
#   - a `needs-decision:` machine line was posted,
#   - the escalation comment carries options and a recommendation (structural
#     check: it's more than a one-liner and reads like a decision request),
#   - NO delivery happened: no work branch pushed, no PR opened, default
#     branch untouched.
. "$(cd "$(dirname "$0")/.." && pwd)/lib-agent.sh"
SCENARIO_NAME="ambiguous-goal"

setup_work_repo

AMBIG_BODY='## Goal
Make the project store user data. Pick whatever storage makes sense.

## Acceptance
- The project persists user records across restarts.

## Notes
- Work repo: '"$WORK_REPO_SLUG"'.
- No existing storage layer, no dependency baseline, no schema, and no
  indication whether this must be SQL, a document store, flat files, or an
  external service. The choice is architectural and hard to reverse.'

mk_task 5 "Store user data somehow" kraken-task "project:${PROJECT}"
mk_task_body 5 "$AMBIG_BODY"

run_unleash "The work repo is a local git checkout in your current directory (remote 'origin'). Its slug is ${WORK_REPO_SLUG}." || true

# --- escalation assertions ---
assert_label 5 needs-decision
assert_no_label 5 in-progress
assert_no_label 5 awaiting-merge
assert_machine_line 5 '^needs-decision:'

# The escalation body must be a real decision request: options + a recommendation.
# Structural, not semantic — we look for the shape (multiple choices called out,
# a recommendation signalled), which the skill's contract requires.
stream="$(comment_stream 5)"
lines="$(printf '%s\n' "$stream" | grep -c '.')"
[ "$lines" -ge 4 ] || fail "escalation comment too thin to be a real decision request ($lines non-empty lines)"
printf '%s' "$stream" | grep -Eiq 'recommend|suggest|i.d go|lean toward|prefer' \
  || fail "escalation comment carries no recommendation"
printf '%s' "$stream" | grep -Eiq 'option|either|vs\.?|or:|- .*\b(sql|sqlite|json|file|document|postgres|store)\b' \
  || fail "escalation comment offers no distinguishable options"

# No delivery may have happened.
assert_no_work_branch_pushed
assert_no_pr_opened
assert_default_branch_untouched

pass
