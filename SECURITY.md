# Security

## The core fact

**A kraken task is untrusted input that executes with your credentials.**

Anyone who can open an issue in your coordination repo is, in effect, writing
instructions that a Claude Code worker will carry out in your prepared
environment — reading your files, running your toolchain, and pushing branches
authenticated as you. A task body (Goal / Acceptance / Notes) is free text an
agent acts on; it cannot reliably tell a legitimate task from an injected one.

Treat **write access to the coordination repo as equivalent to shell access to
every worker environment pointed at it.** Every hardening decision below follows
from that one sentence.

## Threat model

**What you're protecting:** your credentials (the `gh` token and git push
rights a worker holds), your work repos, and the environment each worker runs in
— its source, its secrets, and any services it can reach.

**Primary threat — prompt injection via a task body.** A malicious or careless
task can try to steer a worker into exfiltrating secrets, pushing to an
unexpected repo, or running destructive commands. The injector is anyone with
issue-open access to the coordination repo, and the input is untrusted *always*.

**Secondary threats:**

| Threat | Vector |
| --- | --- |
| Untrusted work repo / dependency | Cloning and building a repo runs its code (build scripts, test/postinstall hooks) in the worker environment |
| Data exfiltration | A worker with network access can send environment contents anywhere |
| Resource abuse | A task crafted to burn tokens or compute |
| Confused deputy on delivery | A command allowlist cannot, by itself, tell a work branch from the default branch |

## What Kraken's design already gives you

These are conventions the `unleash` skill and your allowlist enforce — **not a
sandbox.** They shrink the blast radius; they are not the hard guarantee.

- Workers deliver on **draft PRs only** — the worker never merges. Merging is
  always yours.
- Workers **never push to the default / protected branch.**
- `gh pr merge` is deliberately kept **off** the recommended allowlist.
- An ambiguous task goes to `needs-decision`, not improvisation.
- The coordination repo holds **issues only, never code.**

The hard guarantees come from the operator controls below.

## Hardening checklist

1. **Keep the coordination repo private, and restrict write access to people you
   trust.** Its write access *is* command over your workers. (See the README FAQ,
   "Who can command my workers?")
2. **Protect the work repo's default branch** (required review). This is the hard
   guarantee that no worker can land code on its own — an allowlist alone cannot
   distinguish a work branch from the default branch, so enforce it server-side.
3. **Scope credentials tightly.** Prefer a **fine-grained PAT** limited to the
   specific repos and the minimum permissions — `contents: write` on the work
   repos, `issues: write` on the coordination repo — never a classic token with
   access to every repo you own.
4. **Isolate the worker.** Run each worker in a **disposable container or VM**
   with only the repos, secrets, and network it needs. Workers that share test
   state (database, fixtures, ports) cannot share an environment: fully isolated
   environments per worker, or one worker.
5. **Keep the allowlist tight.** Pre-allow only what delivery needs — `git
   add`/`commit`/`push`, `gh pr create`, and the project's test runner. See the
   worker-environment permissions example in the README.
6. **A human reads every diff before merge.** The draft PR is the review gate;
   the value of "never merge" is that you get to look first.

## What to never pre-allow

An allowlist runs unattended, with nobody to catch a bad call. Keep all of these
behind an interactive ask-gate (or off the machine entirely):

- `gh pr merge`, `gh pr review --approve` — landing work stays a human decision.
- `git push` to a default or protected branch (protect the branch server-side —
  an allowlist cannot tell branches apart).
- Deploy / publish / release commands — `npm publish`, `terraform apply`,
  `gh release create`, deployment triggers.
- Destructive commands — `rm -rf` outside the workspace, `git push --force`,
  branch or repo deletion (`gh repo delete`).
- Access to credentials or secrets beyond what the task legitimately needs.

## Reporting a vulnerability

Please report security issues **privately**, not as a public issue. Use GitHub's
private vulnerability reporting — the **"Report a vulnerability"** button under
this repository's **Security** tab — which opens a private advisory visible only
to the maintainers. We'll acknowledge and work a fix from there.
