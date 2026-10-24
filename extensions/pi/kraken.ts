/**
 * kraken.ts — the kraken extension for Pi (pi.dev). Optional.
 *
 * Load it with Pi's `extensions` settings key (or `-e <path>`):
 *   "extensions": ["/path/to/kraken/extensions/pi/kraken.ts"]
 * Pi compiles it at load time (jiti): no build step, no package.json, no npm.
 *
 * An event-to-bash adapter with two handlers. Every decision lives in a file
 * `make test` covers, never in here:
 *
 * - session_shutdown: hands this session's claims back, the way the
 *   SessionEnd hook does for Claude Code and Copilot CLI. It pipes the same
 *   SessionEnd-shaped event into hooks/session-end-release.sh, so the release
 *   (detached, best-effort, scoped) is the one hooks/lib-release-claims.sh
 *   already implements. Scope: the session id. Pi's bash tool exports
 *   PI_SESSION_ID, `kraken.py claim` records it in the claim state file, and
 *   the hook frees only claims naming this session, so another session's or
 *   worker's live claim is never touched (#173). Skipped on `reload` (the
 *   same session goes on) and `fork` (the conversation goes on in the fork).
 *
 * - tool_call: refuses a `bash` command that runs `gh pr merge`,
 *   `gh repo delete` or `gh issue close` — the shell half of the deny rules
 *   Copilot CLI gets from KRAKEN_COPILOT_FLAGS. The matcher is
 *   extensions/pi/deny.py. Defense in depth, NOT a sandbox: see deny.py for
 *   what it does not see. A check that fails blocks the call (fail closed).
 *
 * Like the hooks, this is an OPTIMIZATION over lease expiry, never the
 * recovery mechanism (PROTOCOL.md §5): without it a Pi worker still conforms,
 * and an abandoned claim still frees itself within one TTL.
 */
import { spawn } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

// The checkout this file sits in (<kraken>/extensions/pi/kraken.ts), so the
// extension works from any clone without an env var to set.
const KRAKEN_ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SESSION_END_HOOK = join(KRAKEN_ROOT, "hooks", "session-end-release.sh");
const DENY = join(KRAKEN_ROOT, "extensions", "pi", "deny.py");

// Run a command with `input` on stdin; resolve with its exit code (null when
// it could not start or was killed) and stdout. Never rejects.
function run(cmd: string, args: string[], input: string, timeout: number) {
  return new Promise<{ code: number | null; stdout: string }>((resolve) => {
    const child = spawn(cmd, args, { stdio: ["pipe", "pipe", "ignore"], timeout });
    let stdout = "";
    child.stdout.on("data", (chunk) => (stdout += chunk));
    child.on("error", () => resolve({ code: null, stdout }));
    child.on("close", (code) => resolve({ code, stdout }));
    child.stdin.on("error", () => {});
    child.stdin.end(input);
  });
}

export default function (pi: ExtensionAPI) {
  pi.on("tool_call", async (event) => {
    if (event.toolName !== "bash") return undefined;
    const command = typeof event.input.command === "string" ? event.input.command : "";
    const { code, stdout } = await run("python3", [DENY], command, 10_000);
    if (code === 0) return undefined;
    if (code === 2) return { block: true, reason: stdout.trim() };
    return { block: true, reason: `kraken: the deny check (${DENY}) failed (exit ${code}), so the command is refused.` };
  });

  pi.on("session_shutdown", async (event, ctx) => {
    if (event.reason === "reload" || event.reason === "fork") return;
    const session_id = ctx.sessionManager.getSessionId();
    if (!session_id) return;
    const hookEvent = JSON.stringify({ hook_event_name: "SessionEnd", reason: event.reason, session_id });
    await run("bash", [SESSION_END_HOOK], hookEvent, 5_000);
  });
}
