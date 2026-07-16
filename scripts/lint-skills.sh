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
TEMPLATE="skills/unleash/task-template.yml"
REAPER="skills/unleash/reclaim-stale.yml"
# The seven transition scripts were consolidated into one stdlib-only program;
# the *.sh files are now thin shims that exec into it. Every label / machine-line
# / disclaimer emitter therefore lives in kraken.py, so all the per-emitter
# checks below resolve to that single module.
KRAKEN="skills/unleash/kraken.py"
WATCHER="$KRAKEN" # label filter delegated to $LISTER
LISTER="$KRAKEN"
CLAIM="$KRAKEN"
RELEASE="$KRAKEN"
ESCALATE="$KRAKEN"
DELIVER="$KRAKEN"
HEARTBEAT="$KRAKEN" # comments only — touches no labels

fail=0
err() { printf '  \033[31mx\033[0m %s\n' "$1"; fail=$((fail+1)); } # count, so per-section fail_before diffs stay accurate
note() { printf '  · %s\n' "$1"; }

# --- 1. Label strings match across every file that hard-codes them ----------
echo "[1] label consistency"
check_label() {
  local label="$1"; shift
  local missing=""
  for f in "$@"; do grep -qF -- "$label" "$f" || missing="$missing $f"; done
  [ -n "$missing" ] && err "label '$label' missing from:$missing"
}
# init creates all four; status surfaces only the three human-facing labels (no
# kraken-task); the lister owns the startable filter (all four); claim guards on
# the three held labels; escalate/deliver each swap in-progress for their target;
# release only touches in-progress
check_label "kraken-task"    "$SKILL" "$INIT" "$README" "$PROTOCOL" "$TEMPLATE" "$LISTER"
check_label "in-progress"    "$SKILL" "$INIT" "$STATUS" "$README" "$PROTOCOL" "$REAPER" "$LISTER" "$CLAIM" "$RELEASE" "$ESCALATE" "$DELIVER"
check_label "needs-decision" "$SKILL" "$INIT" "$STATUS" "$README" "$PROTOCOL" "$REAPER" "$LISTER" "$CLAIM" "$ESCALATE"
check_label "awaiting-merge" "$SKILL" "$INIT" "$STATUS" "$README" "$PROTOCOL" "$LISTER" "$CLAIM" "$DELIVER"
# common typo class: labels use hyphens, never underscores
for bad in kraken_task in_progress needs_decision awaiting_merge; do
  grep -qInF -- "$bad" "$SKILL" "$INIT" "$STATUS" "$README" "$PROTOCOL" "$TEMPLATE" "$REAPER" "$WATCHER" "$LISTER" "$CLAIM" "$RELEASE" "$ESCALATE" "$DELIVER" "$HEARTBEAT" 2>/dev/null \
    && err "underscore variant '$bad' found (labels use hyphens)"
done
[ "$fail" -eq 0 ] && note "4 canonical labels aligned across files"

# --- 1b. Machine lines: the spec and every emitter agree ---------------------
echo "[1b] machine-line consistency (PROTOCOL.md vs emitters)"
fail_before=$fail
check_label "claimed-by:"     "$PROTOCOL" "$SKILL" "$CLAIM"
check_label "heartbeat:"      "$PROTOCOL" "$SKILL" "$HEARTBEAT"
check_label "needs-decision:" "$PROTOCOL" "$SKILL" "$ESCALATE" "$CLAIM"
check_label "delivered:"      "$PROTOCOL" "$SKILL" "$DELIVER" "$CLAIM"
check_label "released:"       "$PROTOCOL" "$SKILL" "$RELEASE" "$CLAIM"
# stale-claim: is the reaper's line — the skill drives no emitter of it
check_label "stale-claim:"    "$PROTOCOL" "$REAPER" "$CLAIM"
[ "$fail" -eq "$fail_before" ] && note "6 machine lines aligned across spec, skill, and emitters"

# --- 1c. Attribution disclaimer: byte-identical everywhere -------------------
# The blockquote is defined once in kraken.py (the single emitter) plus the
# skill and the spec. Normalize the code form (the {worker} placeholder, a
# trailing string-literal quote) to the doc form and require equality.
echo "[1c] attribution disclaimer consistency"
fail_before=$fail
canon_disclaimer() {
  # LC_ALL=C: GNU grep 3.1 (Git Bash's build) won't match the astral-plane 🐙
  # (U+1F419) under a UTF-8 locale, so a local run false-fails "no disclaimer
  # found" on prose that has it. The disclaimer is a fixed byte string, so
  # bytewise matching is exact (and matches how CI's newer grep behaves).
  LC_ALL=C grep -m1 -o '> 🐙 .*' "$1" \
    | sed -e 's/\\`/`/g' -e 's/${WORKER}/<worker-name>/g' \
          -e 's/{worker}/<worker-name>/g' -e 's/"$//' -e 's/\r$//'
}
ref="$(canon_disclaimer "$SKILL")"
if [ -z "$ref" ]; then
  err "no attribution disclaimer found in $SKILL"
else
  for f in "$CLAIM" "$HEARTBEAT" "$ESCALATE" "$DELIVER" "$RELEASE" "$PROTOCOL"; do
    [ "$(canon_disclaimer "$f")" = "$ref" ] || err "attribution disclaimer drift in $f"
  done
fi
[ "$fail" -eq "$fail_before" ] && note "disclaimer identical across skill, spec, and kraken.py"

# --- 1d. Claim-window resets: code never ahead of the spec -------------------
# Every keyword kraken.py treats as a window reset must appear as a machine line
# in PROTOCOL.md's table. Catches the dangerous drift direction: a new reset
# added to the implementation without the contract learning about it.
echo "[1d] claim-window reset keywords (kraken.py vs PROTOCOL.md)"
fail_before=$fail
reset_line="$(grep -E 'WINDOW_RESET_PREFIXES *=' "$CLAIM")"
resets="$(printf '%s' "$reset_line" | grep -oE '[a-z-]+:')"
if [ -z "$resets" ]; then
  err "could not extract the reset case pattern from $CLAIM"
else
  n=0
  for kw in $resets; do
    n=$((n+1))
    grep -qF -- "\`${kw}" "$PROTOCOL" || err "claim-window reset '$kw' in kraken.py missing from PROTOCOL.md"
  done
fi
[ "$fail" -eq "$fail_before" ] && note "$n reset keyword(s) in claim.sh all specified in PROTOCOL.md"

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
for sh in scripts/*.sh skills/*/*.sh tests/*.sh tests/t/*.sh tests/gh-stub/gh; do
  [ -f "$sh" ] || continue
  bash -n "$sh" 2>/dev/null || err "$sh has a bash syntax error"
done
if command -v python3 >/dev/null 2>&1; then
  for py in skills/*/*.py tests/unit/*.py; do
    [ -f "$py" ] || continue
    python3 -m py_compile "$py" 2>/dev/null || err "$py has a python syntax error"
  done
fi
if command -v python3 >/dev/null 2>&1 && python3 -c 'import yaml' >/dev/null 2>&1; then
  for y in "$TEMPLATE" "$REAPER" .github/workflows/*.yml; do
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
