# lib-copilot-drain.sh — the ONE definition of how a GitHub Copilot CLI
# tentacle is asked to do a drain pass: the prompt and the permission flags.
# Source it, don't execute it. Shared by the ambush loop (scripts/kraken-loop.sh),
# the interactive launcher (scripts/kraken-copilot.sh) and the agent-behavior
# harness (tests/agent/lib-agent.sh), so the harness
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
# The built-in GitHub MCP server is a second road to the same writes, around
# the shell: its default CLI toolset is read-only today, but an operator who
# widens it (--enable-all-github-mcp-tools) must not widen the worker's
# authority with it — merging stays denied, and so does issue_write, which can
# close a task (a worker's transitions all run through kraken.py).
KRAKEN_COPILOT_FLAGS=(
  --allow-all-tools --no-ask-user
  "--deny-tool=shell(gh pr merge:*)"
  "--deny-tool=shell(gh repo delete:*)"
  "--deny-tool=shell(gh issue close:*)"
  "--deny-tool=github-mcp-server(merge_pull_request)"
  "--deny-tool=github-mcp-server(issue_write)"
)

# kraken_require_github_auth NAME — succeed when kraken.py can get a token the
# way it looks for one (GH_TOKEN, then GITHUB_TOKEN, then `gh auth token`), or
# say how to fix it on stderr and fail. Without one every queue read fails, and
# a launcher that let that through would sit on what looks like an idle queue.
# The `gh` has to be the one where this script runs: a Linux shell (WSL) does
# not see a gh.exe installed on the Windows side.
kraken_require_github_auth() {
  [ -z "${GH_TOKEN:-}${GITHUB_TOKEN:-}" ] || return 0
  if command -v gh >/dev/null 2>&1 && gh auth token >/dev/null 2>&1; then
    return 0
  fi
  echo "$1: no GitHub token for kraken.py — install gh here and run 'gh auth login'," \
       "or export GH_TOKEN (see the README's note on GH_TOKEN and Copilot's own login)." >&2
  return 1
}

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
