#!/usr/bin/env bash
# PROTOCOL.md §8 L229: "Every delivered commit MUST carry the attribution
# trailers" — the agent's own `Co-Authored-By:` and the authoritative
# `Kraken-Task: <repo>#<issue> (worker: <name>, kraken@<version>)`. Every other
# tests/t/*.sh models `gh` but no git, so a delivery that dropped the trailers
# would pass the suite (COVERAGE.md gap G2). This is the first case that needs
# real git: it stands up a throwaway local work repo, builds delivery commits the
# way a worker does — appending both trailers, the Kraken-Task line taken verbatim
# from `kraken.py contract task-trailer` (its single source of truth) — and
# asserts every commit on the delivered branch carries both, well-formed, read
# through git's OWN trailer parser (%(trailers:...)). A negative commit with no
# trailers proves the assertion actually fails when a trailer is missing.
. "$ROOT/tests/lib.sh"

# git is the one new dependency this harness needs beyond the suite's jq/python3.
# Absent = skip (exit 0), matching run-tests.sh's skip-on-minimal-machine policy.
command -v git >/dev/null 2>&1 || { echo "git-trailers: SKIP (git not found)"; exit 0; }

REPO_SLUG="OWNER/tasks"   # coordination repo the worker was invoked with
ISSUE=7
WORKER=w1
# The agent's own identity line (this suite's driver is GitHub Copilot; the
# format is the agent's, PROTOCOL.md §8 keeps it the worker's to compose).
COAUTHOR="Co-Authored-By: GitHub Copilot <noreply@github.com>"

# The Kraken-Task trailer is NOT hand-written here: it comes verbatim from
# kraken.py, the single source of its format and its kraken@<version> stamp.
# Pinning the commit against this output is what ties the trailer's worker name
# and version to kraken.py/plugin.json rather than a literal that could drift.
TRAILER="$(python3 "$SCRIPTS/kraken.py" contract task-trailer \
  --repo "$REPO_SLUG" --issue "$ISSUE" --worker "$WORKER")"
[ -n "$TRAILER" ] || fail "kraken.py contract task-trailer produced nothing"

# A fully isolated work repo: its own HOME and empty global/system config, so the
# harness never reads or writes the machine's real git identity or hooks.
WORK="$STATE/work"
mkdir -p "$WORK"
export HOME="$STATE/home"
export GIT_CONFIG_GLOBAL="$STATE/home/.gitconfig-empty"
export GIT_CONFIG_SYSTEM="$STATE/home/.gitconfig-empty-system"
mkdir -p "$STATE/home"
: > "$GIT_CONFIG_GLOBAL"
: > "$GIT_CONFIG_SYSTEM"
export GIT_AUTHOR_NAME="Test Worker" GIT_AUTHOR_EMAIL="worker@example.invalid"
export GIT_COMMITTER_NAME="Test Worker" GIT_COMMITTER_EMAIL="worker@example.invalid"

git -C "$WORK" init -q
git -C "$WORK" checkout -q -b main 2>/dev/null || true
# The repo bootstrap commit — the pre-delivery baseline, exempt from the trailer
# rule (it is not part of the worker's delivery).
echo "baseline" > "$WORK/README"
git -C "$WORK" add -A
git -C "$WORK" commit -q -m "chore: bootstrap"
BASE="$(git -C "$WORK" rev-parse HEAD)"

# commit_delivered FILE LINE — a delivery commit built the way a worker delivers:
# a real code change, then BOTH attribution trailers in the message's trailer
# block. Modeling the delivery discipline is exactly what the conformance stub
# cannot (it has no git).
commit_delivered() {
  printf '%s\n' "$2" >> "$WORK/$1"
  git -C "$WORK" add -A
  git -C "$WORK" commit -q -F - <<EOF
feat: $1 change

Part of the delivered work.

$COAUTHOR
$TRAILER
EOF
}

# A delivered branch of MORE THAN ONE commit — §8 says *every* delivered commit
# carries the trailers, so the harness must check them all, not just the tip.
git -C "$WORK" checkout -q -b "tasks-$ISSUE-delivery"
commit_delivered src.txt "first slice"
commit_delivered docs.txt "second slice"

# trailer_value SHA KEY — the value git's OWN trailer parser extracts for KEY on
# SHA, empty if git does not recognize a well-formed trailer of that key. Reading
# through %(trailers:...) (not a raw grep) is what makes "well-formed" mean
# "git-parseable trailer", not merely "the substring appears somewhere".
trailer_value() {
  git -C "$WORK" log -1 --format="%(trailers:key=$2,valueonly)" "$1" | sed -n '1p'
}

assert_delivered_commit() { # SHA
  local sha="$1" kt ca
  kt="$(trailer_value "$sha" Kraken-Task)"
  ca="$(trailer_value "$sha" Co-Authored-By)"

  [ -n "$ca" ] || fail "commit $sha: Co-Authored-By trailer missing or not git-parseable"
  [ -n "$kt" ] || fail "commit $sha: Kraken-Task trailer missing or not git-parseable"

  # Well-formed Kraken-Task: <repo>#<issue> (worker: <name>, kraken@<version>).
  printf 'Kraken-Task: %s\n' "$kt" \
    | grep -qE '^Kraken-Task: [^ ]+#[0-9]+ \(worker: [^,]+, kraken@[^ )]+\)$' \
    || fail "commit $sha: Kraken-Task malformed [$kt]"
  # The kraken@<version> stamp must be a real version, never the "unknown"
  # fallback kraken.py emits when it cannot read the manifest.
  case "$kt" in *"kraken@unknown"*) fail "commit $sha: Kraken-Task version is 'unknown' [$kt]";; esac
  # Worker name and full line agree with kraken.py's authoritative output — no
  # drift between what the worker committed and what the contract mandates.
  case "$kt" in *"worker: $WORKER,"*) : ;; *) fail "commit $sha: worker name not '$WORKER' [$kt]";; esac
  assert_eq "Kraken-Task: $kt" "$TRAILER" "commit $sha Kraken-Task matches kraken.py contract"

  # Co-Authored-By must name a co-author (an addr-spec), not be blank.
  printf '%s\n' "$ca" | grep -qE '<[^>]+@[^>]+>' \
    || fail "commit $sha: Co-Authored-By lacks an email [$ca]"
}

# Every commit the worker delivered (base..tip) carries both trailers.
delivered="$(git -C "$WORK" rev-list "$BASE..HEAD")"
[ -n "$delivered" ] || fail "no delivered commits between base and tip"
count=0
for sha in $delivered; do
  assert_delivered_commit "$sha"
  count=$((count + 1))
done
[ "$count" -ge 2 ] || fail "expected >=2 delivered commits checked, got $count"

# Negative control: a commit made WITHOUT the delivery discipline must be caught.
# Proves the assertion above fails when a trailer is absent — a green suite over a
# trailer-dropping delivery is exactly the gap G2 is about.
echo "untrailed" >> "$WORK/oops.txt"
git -C "$WORK" add -A
git -C "$WORK" commit -q -m "feat: forgot the trailers"
BAD="$(git -C "$WORK" rev-parse HEAD)"
if ( assert_delivered_commit "$BAD" ) >/dev/null 2>&1; then
  fail "assertion passed a commit with NO trailers — the harness cannot detect the gap it exists to close"
fi

# Also catch a malformed Kraken-Task (version stamp dropped): git parses the
# trailer, but the well-formedness / contract-match check must reject it.
git -C "$WORK" commit -q --allow-empty -F - <<EOF
feat: malformed trailer

$COAUTHOR
Kraken-Task: $REPO_SLUG#$ISSUE (worker: $WORKER)
EOF
MALFORMED="$(git -C "$WORK" rev-parse HEAD)"
if ( assert_delivered_commit "$MALFORMED" ) >/dev/null 2>&1; then
  fail "assertion passed a malformed Kraken-Task trailer"
fi

exit 0
