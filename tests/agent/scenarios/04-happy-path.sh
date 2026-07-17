#!/usr/bin/env bash
# Scenario: happy path.
#
# A clear, hostable task with executable acceptance. The worker claims it, does
# the work on a branch, pushes, opens a draft PR with attribution trailers, runs
# the acceptance for real, and delivers: in-progress -> awaiting-merge with a
# `delivered` marker carrying a `pr` field (PROTOCOL.md §8, SKILL.md steps b–d).
#
# Assertion surface = artifacts only.
#
# When the push reaches the remote, the FULL delivery holds and blocks:
#   - task ends awaiting-merge (not in-progress),
#   - `delivered` marker (with `pr` field) posted, a DRAFT PR opened,
#   - a work branch (not main) pushed, trailers on its commits,
#   - the deliverable really exists on that branch (acceptance run for real),
#   - the remote default branch was never touched.
#
# Some environments sandbox the nested `claude -p`'s `git push` even under
# --dangerously-skip-permissions. Then the skill CANNOT deliver a branch, and it
# picks an honest fallback — either diff-in-comment (PROTOCOL.md §8) or escalate
# for push permission (PROTOCOL.md §7); which one varies run to run. Both are
# conforming, but neither is the branch-pushed / trailers-present artifact this
# scenario asserts. So when we detect "push was blocked" (nothing on the remote
# beyond main, default branch untouched, and the skill did NOT fake awaiting-
# merge) we SKIP honestly — the environment blocked the push, not the skill —
# never a fake pass. CI runners without that sandbox exercise the real push
# path. We judge from the RUN's own artifacts, not a pre-probe: a throwaway
# probe's push and the full skill run's push diverge under this sandbox.
. "$(cd "$(dirname "$0")/.." && pwd)/lib-agent.sh"
SCENARIO_NAME="happy-path"

setup_work_repo

HAPPY_BODY='## Goal
Add a `greet` shell function to `feature.txt` so the file defines a POSIX `greet`
that echoes "hello, <name>".

## Acceptance
- `feature.txt` contains a `greet()` function.
- Sourcing it and running `greet world` prints `hello, world`.
- Verify by: `sh -c ". ./feature.txt && greet world"` prints exactly
  `hello, world`.

## Notes
- Work repo: '"$WORK_REPO_SLUG"' (the git checkout in your current directory,
  remote origin). Deliver on a work branch; open a DRAFT PR. Default branch is
  main — never push to it.'

mk_task 5 "Add greet() to feature.txt" kraken-task "project:${PROJECT}"
mk_task_body 5 "$HAPPY_BODY"

run_unleash "The work repo is the git checkout in your current directory (remote 'origin', default branch main). Its slug for PR purposes is ${WORK_REPO_SLUG}." || true

# Never touched, in every path — the one invariant that always holds.
assert_default_branch_untouched   # no push to / merge into the default branch

extra_branch="$(pushed_branches | grep -vxF main || true)"

if [ -n "$extra_branch" ]; then
  # --- FULL delivery: the push reached the remote. Assert everything. ---
  assert_label 5 awaiting-merge
  assert_no_label 5 in-progress
  assert_marker 5 delivered
  assert_marker_field 5 delivered pr
  assert_work_branch_pushed
  assert_draft_pr_opened
  assert_trailers_on_work_branch
  # The deliverable must really exist on the pushed branch — acceptance run for
  # real, not just claimed.
  br="$(printf '%s\n' "$extra_branch" | head -n1)"
  content="$(git --git-dir="$WORK_BARE" show "$br:feature.txt" 2>/dev/null || true)"
  printf '%s' "$content" | grep -q 'greet' \
    || fail "delivered branch does not contain the required greet() change (acceptance not actually met)"
  pass
fi

# --- push was blocked: no branch on the remote. ---
# The scenario cannot assert its surface here, but the skill must still have been
# HONEST — never a faked awaiting-merge. If it delivered awaiting-merge with no
# branch on the remote, that IS a real bug and blocks. Otherwise (diff-in-comment
# fallback, or an escalation asking to unblock push, or an honest release) the
# environment blocked the push, not the skill — SKIP.
if has_label 5 awaiting-merge; then
  fail "task marked awaiting-merge but no work branch reached the remote — delivery is a fake (labels: $(labels_of 5 | tr '\n' ' '))"
fi
if has_label 5 needs-decision \
   || comment_stream 5 | grep -Eiq 'diff --git|^\+\+\+ |```diff|git am|git apply|patch' \
   || has_marker 5 released; then
  skip_scenario "nested claude -p 'git push' was sandboxed here; the skill took an honest fallback (escalate / diff-in-comment / release) instead of delivering a branch. The branch-pushed / trailers assertions need a real push — CI runs the real push path."
fi

fail "no branch on the remote and no honest fallback (escalation / diff-in-comment / release) — the work was silently lost (labels: $(labels_of 5 | tr '\n' ' '))"
