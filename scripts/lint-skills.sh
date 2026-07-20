#!/usr/bin/env bash
# Deterministic lint for the kraken skill sources — no tokens, no network.
# Guards the silent-breakage classes that a prose skill is exposed to:
#   label drift across files, orphan "step N" references, task-template field
#   drift, broken relative links/images, and invalid shell/YAML/JSON snippets.
# Runs as a CI gate (.github/workflows/lint.yml) and locally (e.g. a pre-push hook).
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

SKILL="skills/unleash/SKILL.md"
INIT="skills/init/SKILL.md"
STATUS="skills/status/SKILL.md"
README="README.md"
PROTOCOL="PROTOCOL.md"
PLUGIN=".claude-plugin/plugin.json"
TEMPLATE="skills/unleash/task-template.yml"
REAPER="skills/unleash/reclaim-stale.yml"
REQUEUE="skills/unleash/requeue-on-reply.yml"
VALIDATE="skills/unleash/validate-task.yml"
# Every label / machine-line / disclaimer emitter lives in kraken.py, so all the
# per-emitter checks below resolve to that single module.
KRAKEN="skills/unleash/kraken.py"
WATCHER="$KRAKEN"
LISTER="$KRAKEN"
CLAIM="$KRAKEN"
RELEASE="$KRAKEN"
ESCALATE="$KRAKEN"
DELIVER="$KRAKEN"
HEARTBEAT="$KRAKEN"

fail=0
err() { printf '  \033[31mx\033[0m %s\n' "$1"; fail=$((fail+1)); }
note() { printf '  · %s\n' "$1"; }

# --- 1. Label strings match across every file that hard-codes them ----------
echo "[1] label consistency"
check_label() {
  local label="$1"; shift
  local missing=""
  for f in "$@"; do grep -qF -- "$label" "$f" || missing="$missing $f"; done
  [ -n "$missing" ] && err "label '$label' missing from:$missing"
}
# init creates all four; status surfaces only the three human-facing labels;
# the lister owns the startable filter (all four); claim guards the three held
# labels; escalate/deliver each swap in-progress for their target; release only
# touches in-progress.
check_label "kraken-task"    "$SKILL" "$INIT" "$README" "$PROTOCOL" "$TEMPLATE" "$LISTER"
check_label "in-progress"    "$SKILL" "$INIT" "$STATUS" "$README" "$PROTOCOL" "$LISTER" "$CLAIM" "$RELEASE" "$ESCALATE" "$DELIVER"
check_label "needs-decision" "$SKILL" "$INIT" "$STATUS" "$README" "$PROTOCOL" "$LISTER" "$CLAIM" "$ESCALATE"
check_label "awaiting-merge" "$SKILL" "$INIT" "$STATUS" "$README" "$PROTOCOL" "$LISTER" "$CLAIM" "$DELIVER"
# common typo class: labels use hyphens, never underscores
for bad in kraken_task in_progress needs_decision awaiting_merge; do
  grep -qInF -- "$bad" "$SKILL" "$INIT" "$STATUS" "$README" "$PROTOCOL" "$TEMPLATE" "$REAPER" "$WATCHER" "$LISTER" "$CLAIM" "$RELEASE" "$ESCALATE" "$DELIVER" "$HEARTBEAT" 2>/dev/null \
    && err "underscore variant '$bad' found (labels use hyphens)"
done
[ "$fail" -eq 0 ] && note "4 canonical labels aligned across files"

# --- 1b–1c. Contract vocabulary is single-sourced in kraken.py ---------------
# kraken.py OWNS the disclaimer format and marker vocabulary (its DISCLAIMER and
# MARKER_TYPES constants, surfaced by `kraken.py contract`). Rather than diffing
# prose against prose, we EXECUTE the program and check the spec documents
# everything it emits — structural, not byte-equality between files.
if command -v python3 >/dev/null 2>&1; then
  kcontract() { python3 "$KRAKEN" contract "$@"; }

  # [1b] Every marker "type" kraken.py emits is named in PROTOCOL.md's table.
  echo "[1b] marker type vocabulary (kraken.py vs PROTOCOL.md)"
  fail_before=$fail
  while read -r t; do
    [ -z "$t" ] && continue
    grep -qF -- "\`$t\`" "$PROTOCOL" || err "marker type '$t' missing from PROTOCOL.md"
  done < <(kcontract marker-types)
  [ "$fail" -eq "$fail_before" ] && note "every marker type kraken.py emits is specified in PROTOCOL.md"

  # [1c] The disclaimer filter is kraken.py's (unit-tested there); all that
  # remains here is that the docs still quote it illustratively.
  echo "[1c] attribution disclaimer quoted in the docs"
  fail_before=$fail
  for f in "$PROTOCOL" "$SKILL"; do
    LC_ALL=C grep -qF -- '> 🐙 **Kraken worker' "$f" \
      || err "$f no longer quotes the attribution disclaimer illustratively"
  done
  [ "$fail" -eq "$fail_before" ] \
    && note "docs quote the attribution disclaimer illustratively"

  # [1d] plugin.json's declared protocol version tracks kraken.py's
  # PROTOCOL_VERSION — the drift class that let plugin.json advertise
  # kraken-protocol/3 long after the protocol/4 bump. Executed, not diffed:
  # the constant is the single source, and the declaration must equal it.
  echo "[1d] protocol version (plugin.json vs kraken.py PROTOCOL_VERSION)"
  fail_before=$fail
  pv="$(kcontract protocol-version)"
  declared="$(grep -oE 'kraken-protocol/[0-9]+' "$PLUGIN" | sort -u)"
  if [ -z "$declared" ]; then
    err "$PLUGIN declares no kraken-protocol/<n> version"
  elif [ "$declared" != "kraken-protocol/$pv" ]; then
    err "$PLUGIN declares '$declared' but kraken.py PROTOCOL_VERSION is $pv"
  fi
  [ "$fail" -eq "$fail_before" ] \
    && note "plugin.json declares kraken-protocol/$pv, matching kraken.py"
else
  note "python3 unavailable — skipping contract-derived checks (1b–1d)"
fi

# --- 2. Every "step N" reference points at a step that exists ---------------
echo "[2] step references (in $SKILL)"
max_step="$(grep -oE '^[0-9]+\.' "$SKILL" | tr -d '.' | sort -n | tail -1)"
if [ -z "$max_step" ]; then
  err "no numbered protocol steps found in $SKILL"
else
  bad=""
  while read -r n; do
    [ -z "$n" ] && continue
    { [ "$n" -lt 1 ] || [ "$n" -gt "$max_step" ]; } && bad="$bad $n"
  done < <(grep -oiE 'steps? [0-9]+' "$SKILL" | grep -oE '[0-9]+')
  [ -n "$bad" ] && err "reference(s) to non-existent step(s):$bad (max defined: $max_step)"
  [ -z "$bad" ] && note "all step refs within 1..$max_step"
fi

# --- 3. task-template fields the skill names actually exist -----------------
echo "[3] task-template fields"
for field in goal acceptance notes; do
  grep -qE "id: *$field\b" "$TEMPLATE" || err "template missing field '$field'"
done
[ -z "${bad:-}" ] && note "goal/acceptance/notes present"

# --- 4. Relative links and images resolve (every SKILL.md + README) --------
echo "[4] links & images"
broken=0; total=0
sources=("$README" "$PROTOCOL")
for s in skills/*/SKILL.md; do [ -f "$s" ] && sources+=("$s"); done
# resolve each target against its source file's directory, the way GitHub does
for src in "${sources[@]}"; do
  dir="$(dirname "$src")"
  while IFS= read -r t; do
    [ -z "$t" ] && continue
    case "$t" in http://*|https://*|\#*|mailto:*) continue;; esac
    file="${t%%#*}"; [ -z "$file" ] && continue
    total=$((total+1))
    [ -e "$dir/$file" ] || { err "broken reference in $src: $t"; broken=$((broken+1)); }
  done < <(
    grep -hoE '\]\(([^)]+)\)' "$src" | sed -E 's/^\]\(//; s/\)$//'
    grep -hoE 'src="[^"]+"' "$src" | sed -E 's/^src="//; s/"$//'
  )
done
[ "$broken" -eq 0 ] && note "$total relative link(s)/image(s) resolve"

# --- 5. Shell / YAML / JSON snippets parse ----------------------------------
echo "[5] snippets & assets"
for sh in scripts/*.sh skills/*/*.sh; do
  [ -f "$sh" ] || continue
  bash -n "$sh" 2>/dev/null || err "$sh has a bash syntax error"
done
if command -v python3 >/dev/null 2>&1; then
  # tests/gh-stub/gh is the Python `gh` stub — extensionless so it stays on PATH
  # as `gh`, but Python, so it is syntax-checked here alongside the suite.
  for py in skills/*/*.py tests/*.py tests/unit/*.py tests/conformance/*.py tests/gh-stub/gh; do
    [ -f "$py" ] || continue
    python3 -m py_compile "$py" 2>/dev/null || err "$py has a python syntax error"
  done
fi
if command -v python3 >/dev/null 2>&1 && python3 -c 'import yaml' >/dev/null 2>&1; then
  for y in "$TEMPLATE" "$REAPER" "$REQUEUE" "$VALIDATE" .github/workflows/*.yml; do
    [ -f "$y" ] || continue
    python3 -c 'import sys,yaml; yaml.safe_load(open(sys.argv[1]))' "$y" 2>/dev/null \
      || err "invalid YAML: $y"
  done
else
  note "python3+pyyaml unavailable — skipping YAML parse"
fi
if [ -f .claude-plugin/plugin.json ] && command -v python3 >/dev/null 2>&1; then
  python3 -c 'import sys,json; json.load(open(sys.argv[1]))' .claude-plugin/plugin.json 2>/dev/null \
    || err "invalid JSON: .claude-plugin/plugin.json"
fi

# --- 6. SKILL frontmatter (every skills/*/SKILL.md) -------------------------
# tr -d '\r' so CRLF checkouts (Windows contributors) behave like LF.
echo "[6] frontmatter"
checked=0
for s in skills/*/SKILL.md; do
  [ -f "$s" ] || continue
  checked=$((checked+1))
  skill_lf="$(tr -d '\r' < "$s")"
  if [ "$(printf '%s\n' "$skill_lf" | head -1)" = '---' ]; then
    fm="$(printf '%s\n' "$skill_lf" | sed -n '2,/^---$/p')"
    printf '%s\n' "$fm" | grep -qE '^name:'         || err "$s frontmatter missing name:"
    printf '%s\n' "$fm" | grep -qE '^description:'  || err "$s frontmatter missing description:"
  else
    err "$s does not open with a --- frontmatter block"
  fi
done
[ "$checked" -eq 0 ] && err "no skills/*/SKILL.md found"
[ "$checked" -gt 0 ] && note "$checked skill(s) checked"

echo
if [ "$fail" -ne 0 ]; then
  echo "skill-lint: FAILED"
  exit 1
fi
echo "skill-lint: OK"
