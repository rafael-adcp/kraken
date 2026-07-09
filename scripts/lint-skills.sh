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
README="README.md"
TEMPLATE="skills/unleash/task-template.yml"
REAPER="skills/unleash/reclaim-stale.yml"
WATCHER="skills/watch/watch-queue.sh"

fail=0
err() { printf '  \033[31mx\033[0m %s\n' "$1"; fail=1; }
note() { printf '  · %s\n' "$1"; }

# --- 1. Label strings match across every file that hard-codes them ----------
echo "[1] label consistency"
check_label() {
  local label="$1"; shift
  local missing=""
  for f in "$@"; do grep -qF -- "$label" "$f" || missing="$missing $f"; done
  [ -n "$missing" ] && err "label '$label' missing from:$missing"
}
check_label "kraken-task"    "$SKILL" "$README" "$TEMPLATE" "$WATCHER"
check_label "in-progress"    "$SKILL" "$README" "$REAPER" "$WATCHER"
check_label "needs-decision" "$SKILL" "$README" "$REAPER" "$WATCHER"
check_label "awaiting-merge" "$SKILL" "$README" "$WATCHER"
# common typo class: labels use hyphens, never underscores
for bad in kraken_task in_progress needs_decision awaiting_merge; do
  grep -qInF -- "$bad" "$SKILL" "$README" "$TEMPLATE" "$REAPER" "$WATCHER" 2>/dev/null \
    && err "underscore variant '$bad' found (labels use hyphens)"
done
[ "$fail" -eq 0 ] && note "4 canonical labels aligned across files"

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
sources=("$README")
for s in skills/*/SKILL.md; do [ -f "$s" ] && sources+=("$s"); done
while IFS= read -r t; do
  [ -z "$t" ] && continue
  case "$t" in http://*|https://*|\#*|mailto:*) continue;; esac
  file="${t%%#*}"; [ -z "$file" ] && continue
  total=$((total+1))
  [ -e "$file" ] || { err "broken reference: $t"; broken=$((broken+1)); }
done < <(
  grep -hoE '\]\(([^)]+)\)' "${sources[@]}" | sed -E 's/^\]\(//; s/\)$//'
  grep -hoE 'src="[^"]+"' "${sources[@]}" | sed -E 's/^src="//; s/"$//'
)
[ "$broken" -eq 0 ] && note "$total relative link(s)/image(s) resolve"

# --- 5. Shell / YAML / JSON snippets parse ----------------------------------
echo "[5] snippets & assets"
for sh in scripts/*.sh skills/*/*.sh; do
  [ -f "$sh" ] || continue
  bash -n "$sh" 2>/dev/null || err "$sh has a bash syntax error"
done
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
