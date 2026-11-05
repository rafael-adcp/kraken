# lib-claude-drain.sh — the ONE definition of how a Claude Code tentacle is
# asked to do a drain pass: the slash command and the permission flags. Source
# it, don't execute it. Shared by the ambush loop (scripts/kraken-loop.sh
# --harness claude) and the agent-behavior harness (tests/agent/lib-agent.sh),
# so the harness judges exactly the invocation an operator runs.
#
# The drain is the shared skill itself — `/kraken:unleash ... --once` with this
# checkout loaded as the plugin — never a forked prompt: SKILL.md stays the
# only description of what a drain pass does.

# The permission flags every drain pass runs under. Nobody answers a prompt
# under `claude -p`, so the session runs in `dontAsk` mode: anything not
# allowed is refused on the spot instead of stalling the drain. The allow list
# is the worker's toolbox — the shell, the file tools and subagents (SKILL.md's
# optional context isolation); an MCP tool is refused unless the work repo's
# own .claude/settings.json allows it.
#
# The deny rules put SKILL.md's Authorization boundaries under the model rather
# than in its hands, exactly as KRAKEN_COPILOT_FLAGS does for Copilot: merging
# is always the human's, and a worker never deletes a repo or closes a task —
# whatever a task body says. Claude Code checks deny rules before allow rules,
# so `Bash` allowed whole still refuses these, and it matches each command of a
# compound line (`cd x && gh pr merge 1`) on its own. The GitHub MCP server's
# merge / issue-write tools are denied under the server name Claude Code's docs
# give it (`github`), in case a work repo's settings allow that server.
KRAKEN_CLAUDE_FLAGS=(
  --permission-mode dontAsk
  --allowedTools "Bash,Read,Edit,Write,Glob,Grep,NotebookEdit,Agent"
  --disallowedTools "Bash(gh pr merge:*),Bash(gh repo delete:*),Bash(gh issue close:*),mcp__github__merge_pull_request,mcp__github__issue_write"
)

# kraken_claude_prompt TASKS PROJECT WORKER — print the drain-pass prompt: the
# skill's own slash command, one-shot.
kraken_claude_prompt() {
  printf '/kraken:unleash %s --worker-name %s --project %s --once\n' "$1" "$3" "$2"
}
