#!/usr/bin/env bash
# kraken-copilot.sh — launch an INTERACTIVE GitHub Copilot CLI tentacle.
#
# The interactive counterpart of scripts/kraken-loop.sh: it opens `copilot` in
# the work repo and hands it `/kraken:unleash ...` as its first prompt, under
# the same permission flags the loop's drain passes run with
# (lib-copilot-drain.sh). The session stays open, stays in ambush behind the
# `watch --exit-on-wake` watcher SKILL.md arms, and you can talk to it.
#
# Why a launcher at all: a Claude Code worker gets its unattended permissions
# from the work repo's .claude/settings.json; Copilot CLI takes them as launch
# flags. Typing them by hand every time is how a worker ends up either stalled
# on a permission prompt at 3am or running without the deny rules that keep a
# prompt-injected task from merging, deleting or closing anything.
#
# The skill comes from the kraken plugin installed in Copilot
# (`/plugin install kraken@kraken`). With no such plugin, this checkout is
# loaded as the plugin for the session instead (--plugin-dir), with --add-dir
# so copilot may run its kraken.py.
#
# Usage:
#   scripts/kraken-copilot.sh OWNER/tasks --worker-name <name> --project <name> \
#                             [--work-dir <dir>] [--once] [-- <extra copilot flags>]
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${KRAKEN_REPO_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"

usage() {
  cat <<'EOF'
Usage:
  scripts/kraken-copilot.sh OWNER/tasks --worker-name <name> --project <name> \
                            [--work-dir <dir>] [--once] [-- <extra copilot flags>]

Opens an interactive GitHub Copilot CLI session in the work repo running
/kraken:unleash, with the same permission and deny flags as kraken-loop.sh.

  --worker-name <name>   worker identity in every claim/comment   [KRAKEN_WORKER]
  --project <name>       only take project:<name> tasks           [KRAKEN_PROJECT]
  --work-dir <dir>       the work repo checkout copilot runs in
                         (default: the current directory)        [KRAKEN_WORK_DIR]
  --once                 drain once and stop instead of staying in ambush
  -- <flags>             passed to copilot verbatim (e.g. -- --model auto)
  -h, --help             show this help

The OWNER/tasks coordination-repo slug is positional [KRAKEN_TASKS].
EOF
}

TASKS="${KRAKEN_TASKS:-}"
WORKER="${KRAKEN_WORKER:-}"
PROJECT="${KRAKEN_PROJECT:-}"
WORK_DIR="${KRAKEN_WORK_DIR:-$PWD}"
ONCE=0
EXTRA=()

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help)         usage; exit 0 ;;
    --worker-name)     WORKER="${2:-}"; shift 2 ;;
    --project)         PROJECT="${2:-}"; shift 2 ;;
    --work-dir)        WORK_DIR="${2:-}"; shift 2 ;;
    --once)            ONCE=1; shift ;;
    --)                shift; EXTRA=("$@"); break ;;
    -*)                echo "kraken-copilot: unknown flag: $1" >&2; usage >&2; exit 2 ;;
    *)
      if [ -z "$TASKS" ]; then TASKS="$1"; shift
      else echo "kraken-copilot: unexpected argument: $1" >&2; usage >&2; exit 2; fi
      ;;
  esac
done

missing=""
[ -n "$TASKS" ]   || missing="$missing OWNER/tasks"
[ -n "$WORKER" ]  || missing="$missing --worker-name"
[ -n "$PROJECT" ] || missing="$missing --project"
if [ -n "$missing" ]; then
  echo "kraken-copilot: missing required argument(s):$missing" >&2
  usage >&2
  exit 2
fi

case "$TASKS" in
  OWNER/*|*'<'*|*'>'*)
    echo "kraken-copilot: '$TASKS' looks like the template placeholder — pass your real owner/repo." >&2
    exit 2 ;;
esac

command -v copilot >/dev/null 2>&1 \
  || { echo "kraken-copilot: copilot not found on PATH — install GitHub Copilot CLI first." >&2; exit 1; }
[ -d "$WORK_DIR" ] || { echo "kraken-copilot: --work-dir '$WORK_DIR' is not a directory." >&2; exit 2; }
WORK_DIR="$(CDPATH= cd -- "$WORK_DIR" && pwd)" && cd "$WORK_DIR" \
  || { echo "kraken-copilot: cannot cd into $WORK_DIR" >&2; exit 1; }

. "$REPO_DIR/scripts/lib-copilot-drain.sh"
kraken_require_github_auth kraken-copilot || exit 1

PLUGIN_FLAGS=()
if ! copilot plugin list 2>/dev/null | grep -q 'kraken@'; then
  echo "kraken-copilot: no kraken plugin installed in Copilot — loading $REPO_DIR for this session." >&2
  PLUGIN_FLAGS=(--plugin-dir "$REPO_DIR" --add-dir "$REPO_DIR")
fi

PROMPT="/kraken:unleash $TASKS --worker-name $WORKER --project $PROJECT"
[ "$ONCE" -eq 0 ] || PROMPT="$PROMPT --once"

# The ${a[@]+"${a[@]}"} form: macOS ships bash 3.2, where `set -u` calls an
# empty array unbound.
exec copilot -i "$PROMPT" ${PLUGIN_FLAGS[@]+"${PLUGIN_FLAGS[@]}"} \
  "${KRAKEN_COPILOT_FLAGS[@]}" ${EXTRA[@]+"${EXTRA[@]}"}
