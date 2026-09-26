# lib-copilot-drain.sh — the ONE definition of how a GitHub Copilot CLI
# tentacle is asked to do a drain pass: the prompt and the permission flags.
# Source it, don't execute it. Shared by the ambush loop (scripts/kraken-loop.sh)
# and the agent-behavior harness (tests/agent/lib-agent.sh), so the harness
# judges exactly the invocation an operator runs — never a look-alike that could
# drift from it.

# The permission flags every drain pass runs under. Non-interactive mode needs
# --allow-all-tools; --no-ask-user keeps the worker autonomous (a question it
# cannot ask becomes an escalation on the queue, per SKILL.md). Callers add
# `--add-dir <kraken checkout>` so copilot may read the contract there.
#
# The deny rules put SKILL.md's Authorization boundaries under the model rather
# than in its hands: merging is always the human's, and a worker never deletes a
# repo or closes a task — whatever a task body says. Copilot CLI applies deny
# rules even over --allow-all-tools, so a prompt-injected order to do one of
# these fails at the tool layer instead of relying on the model to refuse it.
# The `:*` suffix is load-bearing: without it a rule matches only the bare
# command, and `gh pr merge 1 --admin` runs (verified on Copilot CLI 1.0.88).
KRAKEN_COPILOT_FLAGS=(
  --allow-all-tools --no-ask-user
  "--deny-tool=shell(gh pr merge:*)"
  "--deny-tool=shell(gh repo delete:*)"
  "--deny-tool=shell(gh issue close:*)"
)

# kraken_copilot_prompt TASKS PROJECT WORKER WORK_DIR KRAKEN_DIR — print the
# drain-pass prompt. The WORK_DIR is where copilot runs (the work repo: code,
# work branch, draft PR); KRAKEN_DIR is the kraken checkout holding the
# contract. The only AGENTS.md copilot auto-loads is the work repo's own, so
# the prompt names the contract files by absolute path.
kraken_copilot_prompt() {
  local tasks="$1" project="$2" worker="$3" work_dir="$4" kraken_dir="$5"
  local kraken_py="$kraken_dir/skills/unleash/kraken.py"
  cat <<EOF
Act as kraken worker $worker, draining project:$project from $tasks.
Your working directory, $work_dir, is the work repo: the code, the work branch and the
draft PR all happen there. Your operating contract lives in the kraken checkout
$kraken_dir — read and follow $kraken_dir/AGENTS.md, $kraken_dir/skills/unleash/SKILL.md and
$kraken_dir/PROTOCOL.md, where <skill> is $kraken_dir/skills/unleash. Do ONE drain pass: run
python3 "$kraken_py" next-action $tasks $project $worker
and do what the envelope says — execute the task it hands you end to end, deliver it as a
draft PR, then stop.
EOF
}
