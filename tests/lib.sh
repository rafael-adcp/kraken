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

# mk_comment N BODY — append a comment in server order.
mk_comment() {
  local d="$STATE/issues/$1/comments"
  printf '%s\n' "$2" > "$d/$(printf '%04d' $(( $(ls "$d" | wc -l) + 1 ))).md"
}

fail() { echo "FAIL: $*"; exit 1; }

assert_eq() { # actual expected context
  [ "$1" = "$2" ] || fail "$3 — expected [$2], got [$1]"
}

assert_rc() { # actual expected context
  [ "$1" -eq "$2" ] || fail "$3 — expected exit $2, got $1"
}

has_label() { grep -qxF -- "$2" "$STATE/issues/$1/labels"; }

comment_count() { ls "$STATE/issues/$1/comments" | wc -l | tr -d ' '; }

last_comment() { cat "$STATE/issues/$1/comments/$(printf '%04d' "$(comment_count "$1")").md"; }
