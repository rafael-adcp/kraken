#!/usr/bin/env bash
# kraken-loop.sh — external "ambush" loop for a GitHub Copilot CLI tentacle.
#
# Copilot CLI has no Monitor tool to arm kraken's zero-token background watcher
# (SKILL.md step 4), so this is the fallback SKILL.md documents: an
# OUTSIDE-the-model shell loop that runs ONE `--once`-style drain pass per
# iteration. A fresh `copilot` process per pass also gives each task the
# fresh-context isolation SKILL.md would otherwise get from a per-task subagent.
#
# Cost control: each poll first runs the FREE, read-only `kraken.py
# list-startable` (one `gh issue list`). The model is only invoked when the queue
# actually has a startable task — an idle queue never spends a token.
#
# The operator owns cadence + stop: Ctrl-C ends it, and it never outlives this
# terminal. This is the versioned home of the loop — run it straight from a
# kraken checkout; no copying it out of a session folder.
#
# Usage:
#   scripts/kraken-loop.sh OWNER/tasks --worker-name <name> --project <name> \
#                          [--poll <seconds>] [--once]
#
# Example:
#   scripts/kraken-loop.sh rafael-adcp/personal-tasks \
#     --worker-name kraken-copilot-env-1 --project kraken
#
# Every flag also has an env-var fallback (KRAKEN_TASKS, KRAKEN_WORKER,
# KRAKEN_PROJECT, KRAKEN_POLL_SECONDS); an explicit flag wins over the env var.
set -u

# --- locate the repo -------------------------------------------------------
# REPO_DIR is derived from this script's own location (scripts/ lives at the
# repo root), so the loop works from any checkout without a hardcoded path.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${KRAKEN_REPO_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
KRAKEN_PY="$REPO_DIR/skills/unleash/kraken.py"

usage() {
  cat <<'EOF'
Usage:
  scripts/kraken-loop.sh OWNER/tasks --worker-name <name> --project <name> \
                         [--poll <seconds>] [--once]

Continuous "ambush" loop for a GitHub Copilot CLI tentacle: each poll runs the
free, read-only `kraken.py list-startable` and only invokes the model when the
queue has a startable task. Ctrl-C to stop; it never outlives this terminal.

Flags (each also has an env-var fallback; an explicit flag wins):
  --worker-name <name>   worker identity in every claim/comment   [KRAKEN_WORKER]
  --project <name>       only take project:<name> tasks           [KRAKEN_PROJECT]
  --poll <seconds>       poll cadence (default 60)          [KRAKEN_POLL_SECONDS]
  --once                 drain once and exit (no polling loop)
  -h, --help             show this help

The OWNER/tasks coordination-repo slug is positional [KRAKEN_TASKS].
EOF
}

# --- config: flags override env vars --------------------------------------
TASKS="${KRAKEN_TASKS:-}"
WORKER="${KRAKEN_WORKER:-}"
PROJECT="${KRAKEN_PROJECT:-}"
POLL="${KRAKEN_POLL_SECONDS:-60}"
ONCE=0

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help)         usage; exit 0 ;;
    --worker-name)     WORKER="${2:-}"; shift 2 ;;
    --project)         PROJECT="${2:-}"; shift 2 ;;
    --poll)            POLL="${2:-}"; shift 2 ;;
    --once)            ONCE=1; shift ;;
    -*)                echo "kraken-loop: unknown flag: $1" >&2; usage >&2; exit 2 ;;
    *)
      if [ -z "$TASKS" ]; then TASKS="$1"; shift
      else echo "kraken-loop: unexpected argument: $1" >&2; usage >&2; exit 2; fi
      ;;
  esac
done

missing=""
[ -n "$TASKS" ]   || missing="$missing OWNER/tasks"
[ -n "$WORKER" ]  || missing="$missing --worker-name"
[ -n "$PROJECT" ] || missing="$missing --project"
if [ -n "$missing" ]; then
  echo "kraken-loop: missing required argument(s):$missing" >&2
  usage >&2
  exit 2
fi

case "$TASKS" in
  OWNER/*|*'<'*|*'>'*)
    echo "kraken-loop: '$TASKS' looks like the template placeholder — pass your real owner/repo." >&2
    exit 2 ;;
esac

[ -f "$KRAKEN_PY" ] || { echo "kraken-loop: cannot find $KRAKEN_PY — run from a kraken checkout." >&2; exit 1; }
cd "$REPO_DIR" || { echo "kraken-loop: cannot cd into $REPO_DIR" >&2; exit 1; }

PROMPT="Act as kraken worker $WORKER, draining project:$PROJECT from $TASKS.
Follow AGENTS.md, SKILL.md and PROTOCOL.md. Do ONE drain pass: claim at most one
startable task, execute it end to end, deliver it as a draft PR, then stop."

drain_pass() {
  local ts startable
  ts="$(date -u +%H:%M:%SZ)"
  if startable="$(python3 "$KRAKEN_PY" list-startable "$TASKS" "$PROJECT" 2>/dev/null)" \
     && [ -n "$startable" ]; then
    echo "kraken-loop: $ts startable task(s) — running a drain pass:"
    printf '%s\n' "$startable" | sed 's/^/  /'
    # Add -s/--silent for terse logs. Drop --no-ask-user only if you want it to
    # be able to escalate to YOU interactively (it can't in a headless loop).
    copilot -p "$PROMPT" --allow-all-tools --no-ask-user
    return 0
  fi
  echo "kraken-loop: $ts queue idle — skipping model."
  return 1
}

if [ "$ONCE" -eq 1 ]; then
  echo "kraken-loop: single drain pass over $TASKS project:$PROJECT as $WORKER."
  drain_pass || true
  exit 0
fi

echo "kraken-loop: watching $TASKS project:$PROJECT as $WORKER (poll ${POLL}s). Ctrl-C to stop."
while true; do
  drain_pass || true
  sleep "$POLL"
done
