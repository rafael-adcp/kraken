#!/usr/bin/env bash
# init conformance (issue #30): the bootstrap kraken.py init mechanizes — verify
# or create the coordination repo PRIVATE, install the five bundled assets via
# the contents API (create / skip-unchanged / flag-customized), and upsert the
# canonical labels — proven against the gh stub with no LLM. Pins: fresh-repo
# bootstrap, idempotent re-run (no repo create, no asset PUT), and the
# flag-don't-clobber path for a customized asset.
. "$ROOT/tests/lib.sh"

BUNDLE="$ROOT/skills/unleash"
kp() { python3 "$SCRIPTS/kraken.py" "$@"; }

# The five (bundled file, destination path) pairs init installs.
ASSET_SRCS=(task-template.yml reclaim-stale.yml cleanup-closed.yml requeue-on-reply.yml validate-task.yml)
ASSET_DSTS=(
  .github/ISSUE_TEMPLATE/task.yml
  .github/workflows/reclaim-stale.yml
  .github/workflows/cleanup-closed.yml
  .github/workflows/requeue-on-reply.yml
  .github/workflows/validate-task.yml
)

# --- 1. fresh bootstrap: absent repo created private, all assets + labels made -
: > "$STATE/log"
out="$(kp init OWNER/tasks --project app)"
assert_rc $? 0 "fresh init exit"

grep -qF 'repo create OWNER/tasks --private' "$STATE/log" \
  || fail "fresh init did not create the repo PRIVATE"

# Every asset landed byte-identical to its bundled source, and was reported created.
for i in "${!ASSET_SRCS[@]}"; do
  src="$BUNDLE/${ASSET_SRCS[$i]}"; dst="${ASSET_DSTS[$i]}"
  cmp -s "$STATE/contents/$dst" "$src" \
    || fail "asset $dst not installed byte-identical to bundled ${ASSET_SRCS[$i]}"
  printf '%s\n' "$out" | grep -qF "init: asset $dst (created)" \
    || fail "asset $dst not reported created"
done

# The four canonical labels + the project label, each with its canonical color.
for lbl in kraken-task in-progress needs-decision awaiting-merge project:app; do
  [ -f "$STATE/labels-meta/$lbl" ] || fail "label $lbl not upserted"
done
grep -qxF 'color=1D76DB' "$STATE/labels-meta/kraken-task" \
  || fail "kraken-task label lost its canonical color"
grep -qxF 'color=5319E7' "$STATE/labels-meta/project:app" \
  || fail "project:app label lost its canonical purple"

# --- 2. idempotent re-run: repo exists, assets unchanged — no repo create, no PUT
: > "$STATE/log"
out="$(kp init OWNER/tasks --project app)"
assert_rc $? 0 "idempotent re-run exit"

grep -qF 'repo create' "$STATE/log" \
  && fail "re-run wrongly re-created the repo"
grep -qF -- '-X PUT' "$STATE/log" \
  && fail "re-run wrongly re-wrote an asset (PUT on an unchanged file)"
printf '%s\n' "$out" | grep -qF "init: asset ${ASSET_DSTS[0]} (unchanged)" \
  || fail "re-run did not report the task template as unchanged"

# --- 3. flag-don't-clobber: a customized asset is reported, never overwritten ---
: > "$STATE/log"
custom="$STATE/custom.yml"
printf 'name: my customized reaper\n' > "$custom"
mk_content ".github/workflows/reclaim-stale.yml" "$custom"

out="$(kp init OWNER/tasks)"
assert_rc $? 0 "flag-don't-clobber exit"

printf '%s\n' "$out" | grep -qF 'init: asset .github/workflows/reclaim-stale.yml (customized)' \
  || fail "customized asset not flagged"
cmp -s "$STATE/contents/.github/workflows/reclaim-stale.yml" "$custom" \
  || fail "customized asset was overwritten — flag-don't-clobber violated"
grep -qF -- '-X PUT' "$STATE/log" \
  && fail "a PUT was issued during a run where every asset already exists"

exit 0
