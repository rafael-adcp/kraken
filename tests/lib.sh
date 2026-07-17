# Shared helpers for tests/t/*.sh — source, don't execute.
# Gives each test a fresh stub state, the scripts-under-test path, seeding
# helpers, and plain assertions that exit 1 with context on failure.
set -u

SCRIPTS="$ROOT/skills/unleash"
STATE="$(mktemp -d)"
export GH_STUB_STATE="$STATE"
mkdir -p "$STATE/issues"
: > "$STATE/log"
trap 'rm -rf "$STATE"' EXIT

# mk_issue N TITLE [LABEL...] — createdAt derives from N, so a higher number is
# always a younger task (keeps oldest-first assertions readable).
mk_issue() {
  local n="$1" title="$2" d="$STATE/issues/$1"
  shift 2
  mkdir -p "$d/comments"
  printf '%s\n' "$title" > "$d/title"
  echo "open" > "$d/state"
  printf '2026-07-01T%02d:%02d:00Z\n' $((n / 60)) $((n % 60)) > "$d/createdAt"
  : > "$d/labels"
  local l
  for l in "$@"; do printf '%s\n' "$l" >> "$d/labels"; done
}

# mk_comment N BODY [CREATED_AT] — append a comment in server order. An optional
# ISO CREATED_AT is written to a .at sidecar so the reaper's staleness anchor
# (newest worker machine line's createdAt) can be exercised deterministically.
mk_comment() {
  local d="$STATE/issues/$1/comments"
  local seq; seq="$(printf '%04d' $(( $(ls "$d"/*.md 2>/dev/null | wc -l) + 1 )))"
  printf '%s\n' "$2" > "$d/$seq.md"
  [ $# -ge 3 ] && printf '%s\n' "$3" > "$d/$seq.at"
}

# mk_blocked_by N BLOCKER... — record N's native blocked-by relationships
# (the gh-stub's `api .../dependencies/blocked_by` reads this file).
mk_blocked_by() {
  local d="$STATE/issues/$1"
  shift
  : > "$d/blocked_by"
  local b
  for b in "$@"; do printf '%s\n' "$b" >> "$d/blocked_by"; done
}

# mk_body N TEXT — set N's issue body (for the `depends-on: #N` fallback).
mk_body() {
  printf '%s\n' "$2" > "$STATE/issues/$1/body"
}

# mk_pr N STATE [MERGED_AT] — seed a delivery PR the orphan heuristic reads via
# `gh pr view .../pull/N --json state,mergedAt`. STATE is OPEN|MERGED|CLOSED; an
# optional MERGED_AT ISO stamp models a merged PR (absent = null).
mk_pr() {
  mkdir -p "$STATE/pr"
  local merged="null"
  [ $# -ge 3 ] && merged="\"$3\""
  printf '{"state":"%s","mergedAt":%s}\n' "$2" "$merged" \
    > "$STATE/pr/$(printf '%04d' "$1").json"
}

# mk_label LABEL — register a repo label the launch recon may enumerate even
# when no open issue carries it (an empty project:<name>).
mk_label() {
  printf '%s\n' "$1" >> "$STATE/labels"
}

fail() { echo "FAIL: $*"; exit 1; }

assert_eq() { # actual expected context
  [ "$1" = "$2" ] || fail "$3 — expected [$2], got [$1]"
}

assert_rc() { # actual expected context
  [ "$1" -eq "$2" ] || fail "$3 — expected exit $2, got $1"
}

has_label() { grep -qxF -- "$2" "$STATE/issues/$1/labels"; }

comment_count() { ls "$STATE/issues/$1/comments"/*.md 2>/dev/null | wc -l | tr -d ' '; }

last_comment() { cat "$STATE/issues/$1/comments/$(printf '%04d' "$(comment_count "$1")").md"; }

# disclaimer_line WORKER — the authoritative attribution blockquote for WORKER,
# derived from kraken.py's DISCLAIMER constant (the single source of truth) via
# `kraken.py contract`, so no test re-declares the format. A drift in the format
# lands in exactly one place and every test that builds or checks a worker
# comment picks it up automatically.
disclaimer_line() {
  python3 "$SCRIPTS/kraken.py" contract disclaimer --worker "$1"
}

# assert_disclaimer ISSUE WORKER — the attribution blockquote (derived above)
# heads the issue's latest comment. First-line byte-equality against the
# authoritative form, so — unlike a grep for the astral-plane 🐙 (U+1F419), which
# GNU grep 3.1 misses under a UTF-8 locale — there is no locale-sensitive match.
assert_disclaimer() {
  local expected actual
  expected="$(disclaimer_line "$2")"
  actual="$(last_comment "$1" | sed -n '1p')"
  [ "$actual" = "$expected" ] \
    || fail "disclaimer blockquote missing or drifted — expected [$expected], got [$actual]"
}

# assert_marker ISSUE JSON — the issue's latest comment carries the protocol/3
# hidden kraken marker with exactly this compact JSON object (what kraken.py's
# json.dumps(..., separators=(",", ":")) emits). The marker is invisible in the
# rendered UI but present in the raw body.
assert_marker() {
  printf '%s' "$(last_comment "$1")" \
    | grep -qF -- "<!-- kraken $2 -->" \
    || fail "protocol/3 marker '<!-- kraken $2 -->' missing from #$1 latest comment"
}
