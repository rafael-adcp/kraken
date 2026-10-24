# Plan — supporting Pi as a third tentacle harness

Working plan for adding [Pi](https://pi.dev/) (Earendil Inc., `@earendil-works/pi-coding-agent`)
as a kraken worker harness alongside Claude Code and GitHub Copilot CLI. This file is the
resumable state of that effort: check the boxes as they land, and the **Status** section
below says where we left off.

Drafted 2026-10-22 against `kraken@0.7.1`; re-based 2026-10-23 onto `kraken@0.8.0` /
`kraken-protocol/9`. What 0.8.0 changed under this plan (#173–#182):

- **Work dir.** `scripts/kraken-loop.sh` now runs the agent in the **work repo**
  (`--work-dir` / `KRAKEN_WORK_DIR`, default the launch directory), no longer in the kraken
  checkout; copilot reaches the contract via absolute paths in its prompt plus `--add-dir`.
- **Copilot launcher.** `scripts/kraken-copilot.sh` (interactive ambush) and
  `scripts/lib-copilot-drain.sh` (the single-sourced prompt, flags and GitHub-token precheck).
- **Copilot SessionEnd.** Copilot CLI loads `hooks/hooks.json`, so `SessionEnd` now fires for
  Copilot sessions too; the hook is session-scoped (#173) and releases detached.
- **Deny rules.** `KRAKEN_COPILOT_FLAGS` carries `--deny-tool` rules that block `gh pr merge`,
  `gh repo delete`, `gh issue close` and the GitHub MCP merge/issue writes at the tool layer.
  **Pi has no equivalent** — see the deny-parity gap in [Phase 2](#phase-2--self-heal-and-deny-parity-via-a-pi-extension).

## Status — where we left off

**Phase 1: in progress.** PR 1 (`feat/pi-harness-agnostic-loop`, [1.5](#15-scriptskraken-loopsh--generalize-the-harness)
+ the parametrized loop tests), rebased onto 0.8.0, is in review as
[#183](https://github.com/rafael-adcp/kraken/pull/183). The Pi argv it ships is
**unverified against a live Pi** until the deferred spikes 0.3 and 0.9 run.

Next step: **PR 2** — the harness-neutral launch line in `render.py` (1.3). Spikes 0.3 and 0.9
are **deferred: to be run by the operator on Pi+OpenAI**; 0.9 must land before PR 3.
Decisions #1 and #2 are made; #3 and #4 are still open — see [Decisions needed](#decisions-needed).

**Revision history of this plan.** First draft was research-only. Second pass over the repo
found three gaps (the prefix is hardcoded in all three skills; the natural Pi install breaks
`plugin_version()`; Phase 2 had no testing story). This third pass verified every line
reference in the file against the working tree and found **five more**, two of them material:

- **The launch line is emitted by Python, not prose** — `skills/unleash/kraken/render.py:158-168`.
  The old 1.3 blamed `skills/status/SKILL.md:81-82`, which is only the *documented example* of
  that output. PR 2 is therefore not a docs-only PR. See [1.3](#13-the-command-prefix--prose-in-three-skills-and-one-python-emit-site).
- **All three skills end in `$ARGUMENTS`** (`init:126`, `status:149`, `unleash:239`) — a Claude
  Code slash-command interpolation that Pi does **not** perform. This is the one real plumbing
  dependency in an otherwise harness-neutral skill set, and it was missing from the plan
  entirely. See [1.0](#10-arguments--the-one-real-plumbing-dependency).
- `protocol_section()` resolves `SKILL_DIR/../../PROTOCOL.md` (`contract.py:137`), so a copied
  skills tree breaks `kraken.py contract boundary` too — a second, worse consequence of a copy
  install than the version fallback. See [1.2](#12-readmemd--install-and-the-harness-framing).
- Two stale `protocol/6` references in files PR 1 already touches
  (`scripts/kraken-loop.sh:14`, `hooks/session-end-release.sh:9`). *Fixed upstream in 0.8.0.*
- The "no safety net" claim about the launch block was wrong:
  `tests/conformance/test_status.py:73-76` pins its shape.

## The headline

Pi is *more* compatible with kraken than Copilot CLI is on two counts, and less on two.

- It implements the **Agent Skills spec**: a skill directory with `SKILL.md`, frontmatter
  `name` (lowercase, ≤64 chars) + `description` (≤1024), and supporting subdirectories
  (`scripts/`, `references/`, `assets/`) resolved by relative path. Our three skills' frontmatter
  is already conformant — **no fork of the skill bodies**, but see the `$ARGUMENTS` caveat below.
- It auto-loads **`AGENTS.md`** from the agent directory, the working directory *and parent
  directories*, regardless of project trust. Since 0.8.0 the loop runs the agent in the
  **work repo** (`--work-dir`), so what auto-loads is the *work repo's* `AGENTS.md`/`CLAUDE.md`,
  not ours — exactly as on the Copilot path. The contract reaches Pi through
  `--skill <kraken>/skills` instead (Pi has no `--add-dir`).
- It **does not prompt per tool call** ("Pi does not ask for approval before every tool
  call"), so an unattended worker needs no allowlist. Print mode never blocks on project
  trust: without `-a` or a saved decision it simply skips the protected `.pi/` resources
  (`docs/security.md`, "Project trust without an interactive prompt") — so 0.5 is answered
  from the docs and the loop passes no `-a`.
- Its extension API exposes **`session_shutdown`**, the exact analogue of our `SessionEnd`
  hook (which Copilot CLI now fires too, since 0.8.0), and a **`tool_call`** event that can
  block a call — the route to deny-rule parity (Phase 2).
- **What is missing (1):** no plugin marketplace. Install becomes a `settings.json` pointer at
  the kraken checkout, or `--skill <path>`.
- **What is missing (2):** no `$ARGUMENTS` interpolation. Pi appends the text after
  `/skill:name` to the loaded instructions *as a user request* — the arguments do arrive, but
  the three literal `$ARGUMENTS` lines at the end of our skills become dead tokens in the
  prompt. This is the one place a Pi worker reads something our skills wrote for a different
  harness.

### Reference facts gathered (all from `pi.dev/docs/latest`)

| Thing | Value |
| --- | --- |
| Install | `curl -fsSL https://pi.dev/install.sh \| sh`, or `npm i -g --ignore-scripts @earendil-works/pi-coding-agent` |
| Agent (user) config dir | `~/.pi/agent`, overridable via `PI_CODING_AGENT_DIR` |
| Project config dir | `.pi/` in the working directory, loaded **after project trust** |
| Context/system files | `SYSTEM.md`, `APPEND_SYSTEM.md`, `AGENTS.md`, `CLAUDE.md`, `AGENTS.override.md` |
| Resource subdirs | `skills/`, `extensions/`, `prompts/` (slash commands), `themes/` |
| Headless | `-p` / `--print`; `--mode json` for the event stream; also RPC and TypeScript SDK modes |
| Relevant flags | `--skill <path>` (**file or directory, repeatable**), `-e/--extension <path>`, `-t/--tools <list>`, `-nt/--no-tools`, `-a/--approve`, `-na/--no-approve`, `--model`, `--thinking`, `--session`, `-c/--continue` |
| `-a/--approve` | "Trusts project-local configuration and resources **for this process**" — the per-invocation form of `defaultProjectTrust` |
| Relevant settings keys | `skills: string[]` ("skill **files or directories**"), `enableSkillCommands: boolean` (default `true`, "register skills as `/skill:name` commands"), `extensions: string[]`, `defaultProjectTrust: "ask" \| "always" \| "never"` (**agent dir only**), `defaultTools: string[]` |
| Skill discovery | "Pi scans configured skill locations and adds each skill's name, description, and path to the system prompt"; **directories containing `SKILL.md` are discovered recursively** |
| Skill invocation | model-triggered, or explicitly as `/skill:<name>`; "arguments after `/skill:name` are **appended to the loaded instructions as a user request**" — no `$ARGUMENTS`-style interpolation documented |
| `defaultTools` default | `read`, `bash`, `edit`, `write` — **no `grep`/`find`/`ls`**; plain names *replace* the defaults, `+name` adds, `-name` removes |
| Builtin extensions | `builtin:mcp`, `builtin:llama.cpp`, `builtin:codemode`, `builtin:tool-search`, all on by default, disabled with `-builtin:<name>`. **`tool-search` means Pi defers tool schemas** |
| Extension shape | TypeScript in `~/.pi/agent/extensions/`, `export default function (pi: ExtensionAPI)`, compiled at runtime via `jiti` — no build step |
| Lifecycle events | `project_trust`, `session_start`, `session_shutdown`, `resources_discover`, `input`, `before_agent_start`, `agent_start`, `agent_end`, `agent_before_settle`, `agent_settled`, `turn_start`/`turn_end`, `context`, provider events, message events, tool events |
| Security posture | no per-tool allowlist; the docs recommend running "entirely inside a container, virtual machine, or sandbox" |
| Print-mode exit status | `docs/cli-integration.md`: errors go to stderr; a final response with an `error` or `aborted` stop reason, or one cut off at the output limit (`length`), exits **nonzero** |
| Claude-plan OAuth | Pi on Anthropic OAuth is billed as **extra usage, not against plan limits** — `pi -p --model anthropic/claude-sonnet-4-5` returned `400 invalid_request_error: "Third-party apps now draw from your extra usage, not your plan limits."` (exit 1). **PR 3's README must say this.** |

> One documentation inconsistency to be aware of, not to build on: the skills page mentions
> conventional locations (`~/.agents/skills/`, `.agents/skills/`) while the settings page says
> every resource requires explicit configuration. **Plan for the explicit `settings.json`
> pointer**; if the convention turns out to be real it is a nicer install, not a different one.

## Phase 0 — Verification spike (~20min, before any prose)

What the docs do not settle and the plan depends on. Each becomes a line of evidence in the
PR body.

### Resolved without a spike

- [x] ~~**0.1** Does `skills: [...]` accept a parent directory of several skills?~~
  **Yes.** `skills` takes "skill files or directories" and "directories containing `SKILL.md`
  are discovered recursively". One entry pointing at `<kraken>/skills` picks up all three.
- [x] ~~**0.2** Exact command spelling.~~ **`/skill:<name>`**, confirmed on both the skills and
  settings pages (`enableSkillCommands`: "register skills as `/skill:name` commands").
- [x] ~~**0.6** Does `--skill <path>` accept a directory?~~ **Yes** — "loads a skill file or
  directory and is repeatable". The CLI flag and the settings key have the same semantics; no
  contradiction to resolve.
- [x] ~~**0.7** What does the `kraken@<version>` stamp become off a non-checkout install?~~
  **`kraken@unknown`** — and this repo already proves it:
  `tests/unit/test_kraken.py:571` asserts `plugin_version("/no/such/plugin.json") ==
  PLUGIN_VERSION_UNKNOWN`, and `contract.py:132` defines that constant as the literal
  `"unknown"`. No spike needed; 1.2 can state the consequence as fact.

### Still open

- [ ] **0.3** **Print-mode exit codes** — the loop needs to tell "drained" from "died".
  `pi -p "echo hi"; echo $?`, plus a forced-error case (bad model name, unreadable skill path)
  and a mid-run `SIGTERM`.
  **Partial (pi 1.4.0).** Observed exit **1** in three cases: a bad `--model` (`Error: Model
  "…" not found`), a revoked Copilot OAuth (refresh 403), and Anthropic OAuth refusing a
  third-party app (400, see Reference facts); an unreadable `--skill` path did not fail before
  auth. The docs (`cli-integration.md`) now say an `error`/`aborted`/`length` stop reason exits
  nonzero. Still unverified: the success exit code and `SIGTERM` behaviour — **deferred: to be
  run by the operator on Pi+OpenAI**. Either way the loop ignores the exit status (the surviving
  lease decides), so this does not block PR 1.
- [ ] **0.4** Precedence of `AGENTS.md` vs `CLAUDE.md` — **Pi loads both**. *Mostly moot since
  0.8.0:* the loop runs Pi in the work repo, so a loop-driven worker loads the work repo's
  files, never kraken's contributor-facing root `CLAUDE.md`. It only matters for an operator
  who launches Pi by hand from inside the kraken checkout.
- [x] ~~**0.5** Is `-a/--approve` enough for unattended?~~ **Not even needed.** Per
  `docs/security.md`, print mode cannot show the trust prompt; with `defaultProjectTrust`
  `"ask"` (the default) and no saved decision it skips the protected `.pi/` resources and runs.
  Context files and explicit `--skill` paths load regardless. The loop therefore passes no
  `-a`: the work repo's `.pi/` loads only if the operator already trusts it.
- [ ] **0.8** Confirm `builtin:tool-search` actually defers tool schemas in a `-p` run, since
  `skills/unleash/SKILL.md:181-184` already instructs a worker to *load* a missing tool rather
  than conclude it has none. Prerequisite for Phase 3.

### New — found by the third pass

- [ ] **0.9** *(deferred: to be run by the operator on Pi+OpenAI, before PR 3; PR 1's Pi argv
  stays unverified until then)* **How do the three required arguments actually land?** Run
  `pi -p "/skill:unleash acme/tasks --worker-name w1 --project app --once"` and inspect the
  assembled prompt. Two things to record: (a) that the slug/worker/project reach the model as
  an appended user request, and (b) what the trailing literal `$ARGUMENTS` line looks like from
  the model's side. **This is the highest-value item in Phase 0** — if the arguments do not
  arrive, `SKILL.md`'s "do not start, ask for it" rule makes a Pi worker refuse to run, and
  there is no Pi support at all. Feeds decision #4.
- [ ] **0.10** **Does a symlink install preserve the two `../../` resolutions?** Both
  `PLUGIN_MANIFEST` (`contract.py:130-131`) and `PROTOCOL_DOC` (`contract.py:137`) resolve from
  `SKILL_DIR` with `os.path.abspath`, which does **not** follow symlinks — so the symlink's
  *target* decides whether they resolve:

  | Install shape | `SKILL_DIR/../..` becomes | Works? |
  | --- | --- | --- |
  | `skills: ["/path/to/kraken/skills"]` | `/path/to/kraken/` | ✅ |
  | `~/.agents/skills/kraken` → `/path/to/kraken` (repo root) | `~/.agents/skills/kraken/` (through the link) | ✅ expected |
  | `~/.agents/skills/kraken` → `/path/to/kraken/skills` | `~/.agents/skills/` | ❌ both break |
  | `cp -r skills ~/.pi/agent/skills` | `~/.pi/agent/` | ❌ both break |

  Verify rows 2 and 3 for real, then write the **one** shape the install doc recommends.
  Command: `python3 <path>/kraken.py contract task-trailer --repo acme/tasks --issue 1 --worker w1`
  and `python3 <path>/kraken.py contract boundary` — the first must not say `kraken@unknown`,
  the second must not print nothing.

> **0.4 shrank with 0.8.0.** A loop-driven Pi worker runs in the work repo and never sees
> kraken's root `CLAUDE.md`; only a hand-launched Pi inside the kraken checkout would receive
> "run `graphify query` for codebase questions". Decision #3 is now low-stakes.

## Phase 1 — Minimum viable support (the main PR)

### 1.0 `$ARGUMENTS` — the one real plumbing dependency

All three skills end with the same line:

```
Coordination repo / flags / extra context: $ARGUMENTS
```

`skills/init/SKILL.md:126`, `skills/status/SKILL.md:149`, `skills/unleash/SKILL.md:239`.

`$ARGUMENTS` is a **Claude Code slash-command interpolation**. It is not part of the Agent
Skills spec, and Pi's documented behavior is different: arguments after `/skill:name` are
appended to the loaded instructions as a user request. So on Pi the data arrives and the token
does not get substituted — the worker reads a literal `$ARGUMENTS`.

That is a graceful degradation rather than a break (0.9 confirms), but it is exactly the kind
of harness leak `AGENTS.md:46-48` says should not exist in `SKILL.md`. Options, cheapest first:

- [ ] **Keep the line, make it self-describing.** e.g. `Coordination repo / flags / extra
  context: $ARGUMENTS` → a form that reads correctly whether or not it was substituted ("…the
  arguments you were invoked with, below or interpolated here"). One-line change per skill, no
  behavior change on Claude Code.
- [ ] **Alternative:** ship a Pi `prompts/` command that forwards its own arguments into the
  skill, leaving the skill bodies untouched. Interacts with the same decision as 1.3's
  alternative — if we ship `prompts/` for the prefix, it solves this too.

**Blocked on decision #4.** Do not touch three executable prompts before 0.9 says what Pi
actually renders.

### 1.1 `AGENTS.md` — generalize from "Copilot" to "any agents.md-aware CLI"

Today the file reads as if Copilot were its only reader. Full list of Copilot-specific lines,
verified: `:3`, `:26`, `:30`, `:42`, `:55`, `:72`, `:81`, `:87`, `:92`. It becomes
harness-neutral with a small per-harness table at the end.

- [ ] `AGENTS.md:6` — "the **second** implementation whose existence proves the protocol is
  agent-agnostic" → third. Worth making the stronger claim: two harnesses with no deltas is a
  coincidence, three is evidence.
- [ ] `AGENTS.md:25-26` — names *Copilot's* `Co-Authored-By` identity inline. This is a second
  copy of the fact 1.4 changes in `DELIVERY.md:37-39`; both move together or they drift.
  **Blocked on decision #1.**
- [ ] `AGENTS.md:30` — the worker-name example `kraken-copilot-1`. Add a Pi-shaped one.
- [ ] "Driving it non-interactively" (`:50-73`) — today only a `copilot -p` block. Add the Pi
  block:

  ```bash
  pi -p "Act as kraken worker <worker-name>, draining project:<name> from OWNER/tasks.
  Follow AGENTS.md, SKILL.md and PROTOCOL.md. Do ONE drain pass: run kraken.py next-action
  and do what the envelope says — execute the task it hands you end to end, deliver it as a
  draft PR, then stop." --approve
  ```

- [ ] `:72` — "only invokes `copilot` when a task is actually startable" → "the agent".
- [ ] "Self-healing" (`:75-96`) — today it says the hooks "are Claude Code hook events and
  still never fire around a `copilot` process". With Pi this becomes three cases: native
  hooks (Claude Code), an extension (Pi), an external loop (Copilot, and Pi until Phase 2).
  *Re-check against 0.8.0 before PR 3:* Copilot CLI now fires `SessionEnd` too, so this README
  section was rewritten upstream and these line refs are stale.

### 1.2 `README.md` — install and the harness framing

- [ ] `:76-99` **Install** — today "Both agents install from the **same marketplace**". Becomes
  three sections; Pi's is a settings pointer, not a marketplace:

  ```json
  // ~/.pi/agent/settings.json
  {
    "skills": ["/path/to/kraken/skills"],
    "defaultProjectTrust": "always",
    "defaultTools": ["+grep", "+find", "+ls"]
  }
  ```

  Three things that snippet is carrying deliberately:

  - [ ] **The pointer must be a checkout (or a symlink to the repo root) — never a copy, and
    never a symlink to `skills/` alone.** Two separate paths resolve out of `SKILL_DIR/../..`:

    1. `PLUGIN_MANIFEST` (`contract.py:130-131`) → `.claude-plugin/plugin.json`. Missing, and
       `plugin_version()` returns the literal `"unknown"` (`contract.py:132`, pinned by
       `tests/unit/test_kraken.py:571`), so **every delivered commit** carries
       `Kraken-Task: … (worker: X, kraken@unknown)` — destroying exactly the traceability
       `CONTRIBUTING.md:126-128` asks us to keep tied to a protocol revision.
    2. `PROTOCOL_DOC` (`contract.py:137`) → `PROTOCOL.md`. Missing, and `protocol_section()`
       returns `[]` (`tests/unit/test_kraken.py:449`), which means
       `python3 "<skill>/kraken.py" contract boundary` — the command **`SKILL.md:109` tells the
       worker to run** to get its authorization boundaries — prints nothing, silently. That is
       worse than a cosmetic version stamp: a worker asks for the rules and gets an empty
       answer with a zero exit code.

    A third, smaller consequence: the relative links `../../PROTOCOL.md`
    (`skills/unleash/SKILL.md:22`, `DELIVERY.md:4`) and `../../scripts/kraken-loop.sh`
    (`SKILL.md:185`) dangle too. The install section says all of this out loud, in one sentence
    plus the 0.10 table.
  - [ ] `defaultTools` defaults to `read, bash, edit, write` — **no `grep`/`find`/`ls`**. A
    worker doing real coding work wants them. Note the syntax precisely: **plain names replace
    the defaults**, so it must be `["+grep", "+find", "+ls"]` and never
    `["grep", "find", "ls"]` — the latter would strip `bash`, `read`, `edit` and `write` and
    leave the worker unable to deliver anything.
  - [ ] `defaultProjectTrust` is settable **only in the agent directory** — so document it at
    `~/.pi/agent/settings.json`, and mention `-a/--approve` as the per-invocation alternative
    for an operator who does not want a global `"always"`.

- [ ] `:9` (the blockquote), `:127` (the "Worker (tentacle)" row) and `:184-189` ("the GitHub
  Copilot CLI worker reuses it through `AGENTS.md` with **no deltas at all**") — include Pi.
  Note the `:184-189` claim needs the `$ARGUMENTS` caveat from 1.0 if we do not fix it.
- [ ] `:239-241` — "the environment's agent settings must pre-allow the delivery commands …
  (Copilot's equivalent is `--allow-all-tools --no-ask-user`)". **For Pi this sentence is
  false**: there is no permission prompt. Needs an aside — in Pi the risk inverts (everything
  is permitted, which is why Pi's own docs recommend a container/VM), so cite
  `pi.dev/docs/latest/security.md` and recommend an isolated environment. Cross-check
  `SECURITY.md` for whether it makes the same per-tool-allowlist assumption.
- [ ] `:319-327` **Staying in ambush** — Pi also has no Monitor tool, so it joins the
  `kraken-loop.sh` paragraph (until Phase 3).
- [ ] `:564-588` FAQ "Is the Copilot CLI worker as self-healing as the Claude Code one?" →
  generalize into a per-harness parity table.
- [ ] `:377-384` "Why not just use X?" table — **leave alone**. Pi is a harness that becomes a
  tentacle, not an alternative to kraken.
- [ ] **The `/kraken:` command blocks** — 16 sites, not the handful the earlier draft implied:
  `:28`, `:29`, `:88`, `:90`, `:114`, `:115`, `:116`, `:199`, `:281`, `:284`, `:285`, `:293`,
  `:319`, `:342`, `:462`, `:542`. Whatever 1.3 decides about the prefix has to be applied here
  consistently or the README contradicts the skills. Cheapest coherent story: keep `/kraken:`
  in the Claude Code / Copilot sections, and show `/skill:` once in the Pi section with one
  sentence saying the prefix belongs to the harness.

### 1.3 The command prefix — prose in three skills, **and one Python emit site**

`/kraken:<name>` is a Claude-Code plugin spelling. Pi's is `/skill:<name>` (confirmed, 0.2).

**The functional bug is in code, not prose.** `skills/unleash/kraken/render.py:158-168`:

```python
def _launch_lines(repo: str, projects: list[str]) -> list[str]:
    ...
    lines.append(f"     /kraken:unleash {repo} "
                 f"--worker-name <worker-name> --project {name}")
```

- [ ] **`render.py:166` — fix this first.** `_launch_lines` is called from `render.py:74`, so
  **`kraken.py status` prints a Claude-Code-only command** to whatever harness ran it, and
  `skills/status/SKILL.md:146` promises those lines are copy-paste for the human. A Pi operator
  gets a launch line that does not work in their harness. Options: take the prefix from an env
  var / flag, or drop the prefix and print the arguments under a line naming the skill
  (`unleash OWNER/tasks …` + "prefix it the way your harness spells skills").
  **This moves PR 2 out of docs-only territory and gives it a real test.**
- [ ] `tests/conformance/test_status.py:73-76` — asserts `"acme/tasks --worker-name
  <worker-name>"` and `--project <p>` for each project. It does **not** pin `/kraken:`, so the
  change above does not break it — but the new shape must be pinned here in the same PR.
- [ ] `skills/status/SKILL.md:81-82` — the documented *example* of that rendered output.
  Follows `render.py`, never leads it.
- [ ] `skills/status/SKILL.md:146` — "It does NOT invoke `/kraken:unleash` on my behalf".
- [ ] `skills/unleash/SKILL.md:28` — the **Invocation** block. Also `:197`, which offers
  `/loop /kraken:unleash … --once` as the no-watcher fallback (note `/loop` is itself a Claude
  Code facility, so that sentence needs harness scoping regardless).
- [ ] `skills/init/SKILL.md:20` — its own invocation block.
- [ ] `skills/status/SKILL.md:20` — its own invocation block.

Harness-neutral fix for the invocation blocks: show the arguments, and note that the command
*prefix* belongs to the harness, naming both spellings. Keep the wording conservative — this
is executable prose, and `scripts/lint-skills.sh` pins **no** `/kraken:` string anywhere
(verified), so prose changes trip nothing. The only automated net around any of this is
`test_status.py:73-76` on the rendered launch line — which is why the `render.py` emit site is
the safest part of 1.3 to change and the prose is the riskiest.

**Alternative worth weighing first:** Pi exposes `prompts/` as slash commands. Shipping a
prompt there could give Pi operators a kraken-spelled command, leave the skills' invocation
blocks **untouched**, and — if `prompts/` does interpolate arguments — solve 1.0 at the same
time. Cheaper, and it keeps us out of the most dangerous material in the repo. It does not help
`render.py`'s emitted launch block, which still needs fixing either way.

### 1.4 `skills/unleash/DELIVERY.md:37-39` + `AGENTS.md:25-26` — attribution identity

- [ ] `DELIVERY.md:37-39` names Claude Code's and Copilot's `Co-Authored-By` identities. Pi is
  **model-agnostic**, so "Pi" alone misstates who wrote the code. **Blocked on decision #1.**
- [ ] `AGENTS.md:26` repeats Copilot's identity — change both or they drift (see 1.1).
- `PROTOCOL.md` §8 defines the *shape* only and needs no amendment — verified.

### 1.5 `scripts/kraken-loop.sh` — generalize the harness

At 0.8.0, `copilot -p "$PROMPT" --add-dir "$REPO_DIR" "${KRAKEN_COPILOT_FLAGS[@]}"` was
hardcoded in `drain_pass`, and the header and `usage()` described the script as being "for a
GitHub Copilot CLI tentacle". **Landed in PR 1** (line refs below are pre-0.8.0 history):

- [x] New `--harness copilot|pi` flag, defaulting to `copilot` (**backward compatible**), with
  a `KRAKEN_HARNESS` env fallback — matching the flag-or-env pattern already at `:24-25`.
- [x] A per-harness argv table near the `PROMPT` definition, one line per harness. No `eval`
  of a command string.
- [x] A `KRAKEN_AGENT_CMD` escape hatch for an unlisted harness — split on whitespace with
  `read -a` (no quoting, no globbing, no eval), the generic prompt appended last.
- [x] Copilot's argv is byte-for-byte what 0.8.0 ran (shared `KRAKEN_COPILOT_FLAGS`,
  `--add-dir`); Pi's is `pi -p --skill <kraken>/skills -- "/skill:unleash <slug> --worker-name
  <w> --project <p> --once"`, run in the work dir. `SKILL_DIR/../..` still resolves to the
  checkout (the `--skill` path is the real one). The GitHub-token precheck is kraken.py's own,
  so it guards every harness; Pi's model login gets no precheck (`pi auth check` needs a
  provider/model the loop does not know).
- [x] Decide what the loop does with a **non-zero agent exit** once 0.3 answers. Today the
  `copilot` call's status is ignored — `drain_pass` releases and `return 0`s regardless
  (`:136-140`). That is deliberate and correct (a dead drain must still release), so most
  likely this stays as-is and gets a comment saying why rather than new logic.
- [x] The self-heal block (`:97-121`) and `release_own_claim` **do not change** — they are
  harness-agnostic by construction. Only the comments naming the "copilot process" become
  "agent process": `:4`, `:6`, `:16`, `:99`, `:101-102`, `:116`, `:137`, `:139` (the last is a
  release *reason string*, so it shows up in the task thread — worth getting right). **Landed
  as `"<harness> exited mid-drain"`**, so copilot's stays `copilot exited mid-drain` (pinned by
  `tests/e2e/test_copilot_e2e.py`) and a Pi drain says `pi exited mid-drain`.
- [x] ~~`:14` and `hooks/session-end-release.sh:9` say "under protocol/6"~~ — fixed upstream
  in 0.8.0 before PR 1 rebased.
- [x] `hooks/lib-release-claims.sh` described itself as backing "the Copilot ambush loop" —
  generalized, together with the same phrase in `hooks/session-end-release.sh`.
- [x] Rewrite `usage()` and the header comment as "for a tentacle in any harness without a
  native background watcher".
- [ ] **Known gap — no deny-rule parity on Pi.** Pi has no `--deny-tool`, so on Pi merging,
  repo delete, issue close and the GitHub MCP writes are refused by `SKILL.md`'s prose alone,
  not at the tool layer as on copilot. Documented at the Pi argv line; fixed in PR 4 (Phase 2).
- Deviation (PR 1): the `--harness` / `KRAKEN_AGENT_CMD` **validation** runs above the `cd`,
  so a bad value fails before the EXIT trap is armed. The argv table itself sits next to the
  `lib-copilot-drain.sh` sourcing, after the GitHub-token precheck, because copilot's argv needs
  the shared prompt and flags.

### 1.6 Plugin metadata

- [ ] `.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json`: both `description`
  strings say "(Claude Code, GitHub Copilot CLI, or any protocol-conforming tool)" and both
  `keywords` arrays end `claude-code`, `github-copilot`. Add Pi to all four. **Do not touch
  `version`** — `plugin.json` is what the release workflow bumps (`CONTRIBUTING.md:117-121`).

### 1.7 `CONTRIBUTING.md` — the contributor-facing framing

Not in the earlier drafts, and small, but it is the first file a contributor reads:

- [ ] `:4` "three Claude Code skills" → three agent skills.
- [ ] `:9-10` "the protocol between a GitHub-Issues task queue and the Claude Code workers that
  drain it" → harness-neutral.
- [ ] `:51` "`make test-agent` drives **real** `/kraken:unleash --once` runs" — follows
  whatever 1.3 decides, and note the harness is specifically `claude -p` here (by design).
- `DESIGN-REVIEW-2026-08.md:141` also mentions `/kraken:init` — **leave alone**, it is a dated
  design-review record, not live guidance. Worth saying so in the PR body so a reviewer does
  not flag the inconsistency.

## Phase 2 — Self-heal and deny parity via a Pi extension

Since 0.8.0 Copilot has both `SessionEnd` and tool-layer deny rules, so this phase is how Pi
*reaches parity*. A TypeScript extension — versioned in the repo (e.g.
`extensions/pi/`) and pointed at by `settings.json` `extensions: [...]`:

```ts
export default function (pi: ExtensionAPI) {
  pi.on("session_shutdown", async () => { /* bash hooks/lib-release-claims.sh */ });
}
```

- [ ] **Deny-rule parity (the PR 1 gap).** Block the same calls `KRAKEN_COPILOT_FLAGS` denies
  (`scripts/lib-copilot-drain.sh`): `gh pr merge`, `gh repo delete`, `gh issue close` in the
  `bash` tool, and the GitHub MCP merge / issue-write tools. **Verified feasible:** Pi's
  `tool_call` event "can mutate input or block execution" — a handler returns
  `{ block: true, reason }` — and "a `tool_call` handler failure blocks the tool as a fail-safe"
  (`docs/extensions.md`; `examples/extensions/permission-gate.ts`). Match on the command's
  words, like Copilot's `:*` rules, so `gh pr merge 1 --admin` is caught too. Test it the way
  2.1 settles; the copilot side is pinned by `tests/e2e/test_copilot_e2e.py` `DenyRuleTests`.
- [ ] **Reuse `hooks/lib-release-claims.sh` via `bash`** — do not reimplement release logic in
  TypeScript. The extension is a ~30-line shim; the logic stays single-sourced.
- [ ] **Good news on path resolution:** `lib-release-claims.sh:18-20` already falls back from
  `CLAUDE_PLUGIN_ROOT` to `$(dirname "${BASH_SOURCE[0]}")/..`, so an extension that sources it
  by absolute path needs **no change to the library**. The extension does need to know where
  the checkout is — derive it from the extension file's own location, not an env var the
  operator has to set.
- [ ] `session_shutdown` ≡ `SessionEnd`. Pi's docs require idempotent cleanup ("cancellation,
  reload, session replacement, and process exit can converge on the same path") — our release
  already is (a released claim leaves no state file), so this comes free.
- [ ] Pass the worker name through so the release is **scoped**, the way
  `scripts/kraken-loop.sh` `release_own_claim` does with `release_all_claims "$1" "$WORKER"`
  (0.8.0 also added `release_session_claims`, but a Pi claim records no session id). An unscoped
  release in a co-located multi-worker environment would free another worker's live claim —
  the exact failure the loop's second argument exists to prevent.
- [ ] Analogue of the `StopFailure` / `rate_limit` matcher: investigate `agent_end`,
  `after_provider_response` and `provider_stream_event` for a clean rate-limit signal, so the
  extension can stamp the wake-retry flag `kraken.py watch` consumes (`hooks/hooks.json`
  `StopFailure` block, coordination-repo #60). **If there is no clean signal, do not invent
  one** — the lease TTL covers it, and Phase 2 ships as declaredly partial.
- [ ] `hooks/hooks.json` gains a note that its events are Claude-Code-specific and where the
  Pi equivalent lives. (Its `description` already ends "A harness with no hook events is not a
  less conforming worker" — extend that sentence rather than adding a second one.)

### 2.1 How the extension gets tested — settle this *before* writing it

This is the part that collides with a repo invariant, so it belongs in the PR description, not
in review comments. `tests/conformance/test_session_end_release.py` works because the hook is
**shell**: it pipes `{"hook_event_name":"SessionEnd","reason":"exit"}` into
`hooks/session-end-release.sh` and asserts on artifacts. A TypeScript extension is not
testable that way without Node + `jiti` in CI, and `make test` is declaredly stdlib-only ("no
`jq`, no `gh`", `CONTRIBUTING.md:35-43`; "No build, no package manager — just `bash` and
`jq`", `:25`).

The resolution to commit to:

- [ ] Keep the TS shim thin enough that **the tested surface stays
  `hooks/lib-release-claims.sh`**, which the existing conformance tests already pin. The
  extension contributes no release logic of its own — it is an event-to-`bash` adapter.
- [ ] Cover the extension itself with a **hand-run smoke check** (start a session holding a
  claim, kill it, assert the state file is gone), documented in the PR with its output pasted
  in — the same posture `CONTRIBUTING.md:49-63` already takes for `make test-agent`.
- [ ] State explicitly that this is an **optimization over lease expiry, not a normative
  clause** — the wording `hooks/hooks.json` already opens with — so `tests/COVERAGE.md` stays
  untouched and nothing normative rides on untested code.
- [ ] Note that the extension is **not** installed by the Phase 1 settings pointer: `extensions`
  is a separate key with no auto-load directory, so a Phase 1 operator is unaffected by Phase 2
  existing. That is the argument for shipping them as separate PRs.

Without this written down, Phase 2 arrives at review as "a new runtime dependency plus
untested code", which is a fair reason to reject it.

## Phase 3 — Native in-session ambush for Pi (optional, most ambitious)

Today only Claude Code has a zero-token watcher *inside* the session (the Monitor tool). Pi can
have one: extensions may start long-lived processes in `session_start`, and
`agent_before_settle` allows "appending final entries or requesting one last model request
before settlement". That is a buildable Monitor.

- [ ] `session_start` → spawn `kraken.py watch OWNER/tasks <project>`.
- [ ] Read its stdout for the `kraken-queue:` line. Handle the exit-`20` case
  (`KRAKEN_WATCH_MAX_FAILURES` consecutive failed reads, README `:313-315`) as a visible dead
  watcher, never as an idle queue.
- [ ] In `agent_before_settle`, if an event arrived, request the continuation (run **The loop**
  again); otherwise let the session settle.
- [ ] One watcher per worker, never two (`SKILL.md:186-187`) — the extension must not arm a
  second one when a previous drain already armed one.
- [ ] If this works, `SKILL.md:180-187` ("in Claude Code, the Monitor tool … Elsewhere,
  `scripts/kraken-loop.sh`") gains a third path, and Pi reaches **full** parity with Claude
  Code.
- [ ] Mind the deferral (0.8): Pi loads `builtin:tool-search`, so a watcher tool registered by
  the extension may not be in the model's initial tool list. `SKILL.md:181-184` **already**
  handles exactly this case — "if Monitor is not in your tool list, load it first rather than
  concluding you have none; some harnesses defer tool schemas" — which is evidence that
  warning generalizes rather than being a Claude-Code quirk. If Phase 3 lands, that sentence
  should name Pi as the second harness it applies to.

Keep as a separate PR after Phases 1–2 are green — highest discovery risk.

## Tests, lint and CI

- [ ] **`tests/conformance/test_status.py:73-76`** — the launch-line shape assertion. Must move
  with `render.py` in the same PR (1.3). Add a case proving the rendered line carries no
  harness-specific prefix (or carries the configured one).
- [x] **`tests/conformance/test_loop_fast_release.py`** — installs a fake `copilot` on `PATH`
  (`write_copilot_stub`, line 26) and its docstring is Copilot-specific (lines 1-9). Generalize
  to `write_agent_stub(harness, body)` and **parametrize the existing cases over `copilot` and
  `pi`**: crash mid-drain, Ctrl-C/SIGTERM via the trap, clean drain is a strict no-op, stolen
  lease refuses with exit 10. Token-free, runs in CI. Note the stub is invoked with the
  harness's own flags, so the stub writer must take them per harness.
  *Landed in PR 1* as `CopilotLoopTests` / `PiLoopTests` over the four existing release cases
  (the reason string is per harness). The "stolen lease refuses with exit 10" case named here
  **does not exist** in the file, so it was not parametrized. The per-harness *invocation*
  (Pi argv and work dir, `KRAKEN_HARNESS`, unknown harness, the no-eval `KRAKEN_AGENT_CMD`
  split) went into 0.8.0's `tests/conformance/test_loop_work_dir.py` instead, beside the
  copilot argv tests already there.
- [ ] **`tests/conformance/test_deliver_commit_trailers.py:22`** — `COAUTHOR` is hardcoded to
  `GitHub Copilot`. The test asserts *shape* (`<[^>]+@[^>]+>`), not identity, so the constant
  and its comment just follow decision #1 — better, add a case using the Pi identity to prove
  the shape holds.
- [ ] **`tests/unit/test_kraken.py:549-581`** — already pins the `plugin_version()` fallback.
  No change needed; **cite it in the PR body** as the evidence behind 1.2's never-copy rule
  instead of pasting a manual run.
- **`scripts/lint-skills.sh`** — **no change needed for 1.3**: the linter contains no
  `/kraken:` reference at all. What it *does* pin is labels (`:52-58`), protocol version, the
  disclaimer, relative links and snippet syntax. One correction to the earlier draft: its
  `sources` array at `:188` is seeded with `README`/`PROTOCOL`/`AGENTS` and then **auto-extended
  with `skills/*/SKILL.md` and `skills/*/*.md`** (`:189-196`) — so:
  - [ ] only a new **root-level** doc needs adding to `:188` by hand; a new file under
    `skills/*/` joins automatically;
  - [ ] re-run `make lint` after the README/AGENTS rewrites, since both are linted sources and
    the relative-link check (`[4]`) is easy to break while moving sections.
- [ ] **`tests/COVERAGE.md`** — no new normative clause expected, so **no new row**. If 1.3
  touches something COVERAGE cites, update that row.
- [ ] **`tests/agent/`** — three `/kraken:unleash` sites follow 1.3: `lib-agent.sh:98` (the
  real prompt), `README.md:20`, `run-agent-tests.sh:5` (both prose). `lib-agent.sh:105` also
  invokes `claude -p` with `--plugin-dir`/`--dangerously-skip-permissions`, which are
  Claude-Code-only; **leave the harness alone** — this suite is deliberately the Claude Code
  judgment harness. Optionally run one scenario by hand against `pi -p` (the strongest proof
  available: the skill's *judgment* under a different harness), but it spends tokens and never
  runs in CI (`CONTRIBUTING.md:49-63`). Paste the evidence in the PR.
- `Makefile` — unchanged.

## What does NOT change — and that is the point

**`PROTOCOL.md` needs no amendment, there is no integer bump, and no `HISTORY.md` entry.**
Verified rather than assumed: `PROTOCOL.md:7` already states "nothing below requires Claude
Code — any agent …", and `:284` already requires a worker to call itself "a kraken tentacle",
never "a Claude Code tentacle" — a rule `skills/unleash/kraken/comments.py:60` already encodes
in code. The claim algorithm, the lease, the `next-action` envelope, the state record and the
authorization boundaries are all inherited through the same `kraken.py`. If at any point it
feels necessary to write "in Pi, do X differently" inside `SKILL.md`, that is a bug in
`SKILL.md`, not a delta — exactly the rule `AGENTS.md:46-48` already states.

Two caveats, both install-shaped rather than protocol-shaped:

1. §8's commit trailers *are* normative, and 1.2 shows how a careless Pi install degrades the
   `kraken@<version>` half of one to `kraken@unknown` **and** silences
   `kraken.py contract boundary`.
2. `$ARGUMENTS` (1.0) is the one line in the skills written for a specific harness. It is three
   lines of prose, not a protocol clause — but it is the honest answer to "do the skills really
   load with no fork?": the bodies do, the invocation plumbing needs one sentence rewritten.

This whole plan is **docs + one small Python fix + one generalized script + an optional
extension**, with zero protocol change. It is the best evidence the project can show that the
protocol/9 abstraction worked.

## Decisions needed

1. **Decided:** `Co-Authored-By: Pi (<model>) <noreply@pi.dev>`, e.g.
   `Co-Authored-By: Pi (claude-opus-5-5) <noreply@pi.dev>`. Original question below.
   **The Pi worker's `Co-Authored-By` identity.** *(blocks 1.4, 1.1 and the trailers test)* Pi
   is a harness over any provider, so "Pi" alone misstates authorship. Recommendation:
   `Co-Authored-By: Pi (<model>) <noreply@pi.dev>` — names harness and model, and the trailers
   test only requires `Name <email>`. Alternative: keep the model identity (`Claude <model>`)
   and leave the harness to the `Kraken-Task:` trailer. Check whether Earendil publishes a
   convention.
2. **Decided:** Phase 1 alone first, then stacked PRs — one per row of the
   [PR order](#suggested-pr-order) table. Original question below.
   **Scope of the first PR** — Phase 1 alone (Pi works; self-heal via the external loop, same
   as Copilot today), or Phases 1+2 together (exit-hook parity)? Recommendation: **Phase 1
   alone first.** It is prose plus one script plus a small `render.py` fix, reviewable, and
   already yields a correct worker. The TypeScript extension introduces the repo's first
   non-bash runtime dependency (`CONTRIBUTING.md:25`), and that precedent deserves its own PR
   to discuss.
3. **The root `CLAUDE.md`** *(depends on spike 0.4)* — accept the graphify guidance leaking
   into a worker session, or move it into `CONTRIBUTING.md`?
4. **Still open** — decided after spike 0.9, which runs before PR 3.
   **How the Pi worker receives its three required arguments** *(new; depends on spike 0.9,
   blocks 1.0 and interacts with 1.3's alternative)* — rewrite the `$ARGUMENTS` line in all
   three skills to read correctly un-interpolated, or ship a Pi `prompts/` command that
   forwards arguments and leave the skill bodies untouched? Recommendation: **decide after
   0.9**, and prefer the `prompts/` route if it also solves the prefix — one new file beats
   three edits to executable prompts.

## Suggested PR order

| PR | Title (conventional-commit) | Contents |
| --- | --- | --- |
| 1 | `feat(loop): make kraken-loop.sh harness-agnostic` | 1.5 + the parametrized `test_loop_fast_release.py` + Pi cases in `test_loop_work_dir.py` (the `protocol/6` fixes landed upstream in 0.8.0). Mechanical, testable, independent of the prose |
| 2 | `fix(status): render a harness-neutral launch line` | 1.3's `render.py:166` + `test_status.py` assertion. Small, has a test, and is the only *functional* bug in the set — ship it before the prose so the docs can describe real output |
| 3 | `docs: add Pi as a third tentacle harness` | 1.1, 1.2 (incl. the never-copy rule and the `+grep` syntax trap), 1.3's prose sites, 1.4, 1.6, 1.7 + `make lint`. Carries 1.0 if decision #4 lands on editing the skills |
| 4 | `feat(pi): extension for session_shutdown release and deny rules` | Phase 2 — the `session_shutdown` release **and** `tool_call` blocking for deny-rule parity |
| 5 | `feat(pi): native in-session ambush watcher` | Phase 3 |

Branches in the `type/NN-slug` shape keyed to a task or issue, everything in English,
`make check` green (`CONTRIBUTING.md:65-100`).
