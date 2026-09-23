#!/usr/bin/env bash
# kraken-loop.sh — external "ambush" loop for a GitHub Copilot CLI tentacle.
#
# Copilot CLI has no Monitor tool to arm kraken's zero-token watcher (SKILL.md,
# "Staying in ambush"), so this is the documented fallback: an outside-the-model shell loop
# running ONE `--once`-style drain per iteration (a fresh `copilot` process per
# pass also gives each task the fresh-context isolation a subagent would).
#
# Two directories, kept apart: the WORK DIR is where copilot runs (the work
# repo's checkout — the code, the branch and the draft PR live there), and the
# kraken checkout this script sits in is where the operating contract lives
# (AGENTS.md, SKILL.md, PROTOCOL.md, kraken.py). The only AGENTS.md copilot
# auto-loads is the work repo's own, so the prompt names the contract files by
# absolute path and `--add-dir` grants copilot read access to the checkout.
#
# Cost control: each poll first runs the FREE, read-only `kraken.py
# list-startable`; the model is only invoked when a task is actually startable,
# so an idle queue never spends a token. The operator owns cadence + stop
# (Ctrl-C); it never outlives this terminal. Run it straight from a checkout.
#
# Self-heal: under protocol/6 a claim is a LEASE that expires on its own, so an
# abandoned task comes back within one TTL no matter how the drain died. This
# loop just makes it immediate: if a drain dies holding a lease (copilot crash,
# rate-limit abort, Ctrl-C), it releases on the spot via
# hooks/lib-release-claims.sh instead of waiting out the TTL.
#
# Usage:
#   scripts/kraken-loop.sh OWNER/tasks --worker-name <name> --project <name> \
#                          [--work-dir <dir>] [--poll <seconds>] [--once]
#
# Every flag also has an env-var fallback (KRAKEN_TASKS, KRAKEN_WORKER,
# KRAKEN_PROJECT, KRAKEN_WORK_DIR, KRAKEN_POLL_SECONDS); an explicit flag wins
# over the env var. The work dir defaults to the directory you run it from.
set -u

# --- locate the kraken checkout ---------------------------------------------
# REPO_DIR derives from this script's own location, so the loop works from any
# checkout without a hardcoded path. It is where the contract lives, never where
# the work happens — that is WORK_DIR.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${KRAKEN_REPO_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
KRAKEN_PY="$REPO_DIR/skills/unleash/kraken.py"

usage() {
  cat <<'EOF'
Usage:
  scripts/kraken-loop.sh OWNER/tasks --worker-name <name> --project <name> \
                         [--work-dir <dir>] [--poll <seconds>] [--once]

Continuous "ambush" loop for a GitHub Copilot CLI tentacle: each poll runs the
free, read-only `kraken.py list-startable` and only invokes the model when the
queue has a startable task. Ctrl-C to stop; it never outlives this terminal.

Flags (each also has an env-var fallback; an explicit flag wins):
  --worker-name <name>   worker identity in every claim/comment   [KRAKEN_WORKER]
  --project <name>       only take project:<name> tasks           [KRAKEN_PROJECT]
  --work-dir <dir>       the work repo checkout copilot runs in
                         (default: the current directory)        [KRAKEN_WORK_DIR]
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
WORK_DIR="${KRAKEN_WORK_DIR:-$PWD}"
POLL="${KRAKEN_POLL_SECONDS:-60}"
ONCE=0

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help)         usage; exit 0 ;;
    --worker-name)     WORKER="${2:-}"; shift 2 ;;
    --project)         PROJECT="${2:-}"; shift 2 ;;
    --work-dir)        WORK_DIR="${2:-}"; shift 2 ;;
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
[ -d "$WORK_DIR" ] || { echo "kraken-loop: --work-dir '$WORK_DIR' is not a directory." >&2; exit 2; }
# Absolute, so the prompt never names a path relative to a cwd copilot may leave.
WORK_DIR="$(CDPATH= cd -- "$WORK_DIR" && pwd)" && cd "$WORK_DIR" \
  || { echo "kraken-loop: cannot cd into $WORK_DIR" >&2; exit 1; }

# --- fast claim-release (self-heal) ----------------------------------------
# The SessionEnd/StopFailure hooks only fire inside a Claude Code session, so
# this loop is the Copilot harness's equivalent. `kraken.py claim` writes
# $KRAKEN_STATE_DIR/claim-<worker>.json and every terminal transition
# (deliver/escalate/release) removes it, so a file that survives the `copilot`
# process is proof the drain died holding the lease (crash, kill, rate-limit
# abort). Release it on the spot — scoped to THIS worker's lease only — so the
# task requeues in seconds instead of at the lease TTL (PROTOCOL.md §5).
# Best-effort: a failed release falls back to that expiry, which is the actual
# recovery mechanism; this is only the fast path. `release` itself refuses (exit
# 10) if the lease has already been stolen, so a late release can never free a
# task another worker is running.
. "$REPO_DIR/hooks/lib-release-claims.sh"

release_own_claim() { # $1 = reason
  release_all_claims "$1" "$WORKER"
}

# The trap covers the loop itself dying while a drain is in flight: Ctrl-C
# (SIGINT hits the whole foreground group, killing copilot first), kill, or a
# closing terminal. Idempotent — a released claim leaves no state file behind.
trap 'release_own_claim "kraken-loop terminated"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

PROMPT="Act as kraken worker $WORKER, draining project:$PROJECT from $TASKS.
Your working directory, $WORK_DIR, is the work repo: the code, the work branch and the
draft PR all happen there. Your operating contract lives in the kraken checkout
$REPO_DIR — read and follow $REPO_DIR/AGENTS.md, $REPO_DIR/skills/unleash/SKILL.md and
$REPO_DIR/PROTOCOL.md, where <skill> is $REPO_DIR/skills/unleash. Do ONE drain pass: run
python3 \"$KRAKEN_PY\" next-action $TASKS $PROJECT $WORKER
and do what the envelope says — execute the task it hands you end to end, deliver it as a
draft PR, then stop."

drain_pass() {
  local ts startable
  ts="$(date -u +%H:%M:%SZ)"
  if startable="$(python3 "$KRAKEN_PY" list-startable "$TASKS" "$PROJECT" 2>/dev/null)" \
     && [ -n "$startable" ]; then
    echo "kraken-loop: $ts startable task(s) — running a drain pass:"
    printf '%s\n' "$startable" | sed 's/^/  /'
    # Add -s/--silent for terser logs.
    copilot -p "$PROMPT" --add-dir "$REPO_DIR" --allow-all-tools --no-ask-user
    # A lease that survived the copilot process is abandoned — nobody is left
    # to finish or renew it. Free the task now, not at the TTL.
    release_own_claim "copilot exited mid-drain"
    return 0
  fi
  echo "kraken-loop: $ts queue idle — skipping model."
  return 1
}

if [ "$ONCE" -eq 1 ]; then
  echo "kraken-loop: single drain pass over $TASKS project:$PROJECT as $WORKER in $WORK_DIR."
  drain_pass || true
  exit 0
fi

echo "kraken-loop: watching $TASKS project:$PROJECT as $WORKER in $WORK_DIR (poll ${POLL}s). Ctrl-C to stop."
while true; do
  drain_pass || true
  sleep "$POLL"
done
