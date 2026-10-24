#!/usr/bin/env python3
"""deny.py — the deny matcher behind the kraken Pi extension (kraken.ts).

Reads one `bash` tool command on stdin and decides whether it runs a `gh`
command outside a worker's authority (skills/unleash/SKILL.md, Authorization
boundaries): `gh pr merge`, `gh repo delete` or `gh issue close`. These are the
three shell rules `KRAKEN_COPILOT_FLAGS` denies on Copilot CLI
(scripts/lib-copilot-drain.sh); the GitHub MCP half is `--exclude-tools` on the
Pi argv in scripts/kraken-loop.sh, so it needs nothing here.

Exit 0: allowed. Exit 2: denied, with the reason on stdout. Anything else is a
failure of the check, which the extension treats as a block (fail closed).

DEFENSE IN DEPTH, NOT A SANDBOX. It splits the command the way a reader would:
on `;`, `&`, `|`, `(`, `)`, backticks and newlines, outside quotes; drops leading
`NAME=value` assignments and a few transparent prefixes (`env`, `command`,
`exec`, `nohup`, `time`, shell keywords such as `if`/`then`, a `$` before
`(`); and looks at the words that remain. So `gh pr merge 1 --admin`,
`GH_TOKEN=x gh pr merge 1`, `cd w && gh  issue close 3`,
`echo ok; /usr/bin/gh repo delete o/r --yes` and `bash -c "gh pr merge 1"`
are all caught, while `echo "gh pr merge"` and `git commit -m "... gh pr
merge ..."` are not. It does NOT see through a variable (`$GH pr merge`), a
`$(...)` inside double quotes, an alias or function, `eval`, a script file,
another interpreter (`python -c`), or `gh api` calling the merge endpoint
directly. SKILL.md's prose, the human
who merges, and the lease remain the real controls; this only refuses the
plain spelling of a forbidden command, the way Copilot's `--deny-tool` rules
do. A heredoc body line that starts with one of the three commands is a false
positive — the worker can write that file with the write tool instead.
Stdlib only, so `make test` covers it (tests/unit/test_pi_deny.py).
"""
import os
import re
import shlex
import sys

# (subcommand, verb) pairs denied after `gh`, with why. Keep in step with
# KRAKEN_COPILOT_FLAGS in scripts/lib-copilot-drain.sh.
DENIED = {
    ("pr", "merge"): "merging is always the human's",
    ("repo", "delete"): "a worker never deletes a repo",
    ("issue", "close"): "a worker never closes a task; its transitions run through kraken.py",
}
# A token made only of these characters separates two simple commands.
SEPARATOR_CHARS = "();&|`\n"
# Words that sit in front of a command without being it.
PREFIXES = {
    "env", "command", "exec", "nohup", "time", "builtin", "$", "!", "{", "}",
    "if", "then", "else", "elif", "while", "until", "do",
}
SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Only for a command shlex cannot split (unbalanced quotes) — bash would not
# run it either, but the fallback still refuses the plain spelling.
FALLBACK = re.compile(r"\bgh\s+(pr\s+merge|repo\s+delete|issue\s+close)\b")
MAX_DEPTH = 3


def tokens(command):
    """The command's words and separators, or None when it cannot be split.
    Comments are left in: bash starts one only at a word boundary, and a
    commented-out command never reaches the word position checked below."""
    lex = shlex.shlex(command, posix=True, punctuation_chars=SEPARATOR_CHARS)
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        return list(lex)
    except ValueError:
        return None


def simple_commands(words):
    """Split a token list into simple commands at separator tokens."""
    current = []
    for word in words:
        if word and all(c in SEPARATOR_CHARS for c in word):
            if current:
                yield current
            current = []
        else:
            current.append(word)
    if current:
        yield current


def command_words(words):
    """Drop leading assignments and transparent prefixes (and `env`'s own
    options), leaving the command name first."""
    i, after_env = 0, False
    while i < len(words):
        word = words[i]
        if ASSIGNMENT.match(word) or word in PREFIXES:
            after_env = after_env or word == "env"
        elif after_env and word.startswith("-"):
            pass
        else:
            break
        i += 1
    return words[i:]


def program(word):
    name = os.path.basename(word)
    return name[:-4] if name.lower().endswith(".exe") else name


def shell_script(words):
    """The script of `bash -c '<script>'` (any short-option cluster ending in
    `c`, e.g. `-lc`), or None."""
    for i, word in enumerate(words[1:-1], start=1):
        if word == "--":
            return None
        if re.fullmatch(r"-[A-Za-z]*c", word):
            return words[i + 1]
    return None


def denied(command, depth=0):
    """The reason this command is refused, or None."""
    words = tokens(command)
    if words is None:
        m = FALLBACK.search(command)
        if not m:
            return None
        return reason(tuple(m.group(1).split()))
    for simple in simple_commands(words):
        cmd = command_words(simple)
        if not cmd:
            continue
        name = program(cmd[0])
        if name == "gh" and tuple(cmd[1:3]) in DENIED:
            return reason(tuple(cmd[1:3]))
        if name in SHELLS and depth < MAX_DEPTH:
            script = shell_script(cmd)
            if script is not None:
                found = denied(script, depth + 1)
                if found:
                    return found
    return None


def reason(pair):
    return ("kraken: `gh %s %s` is outside a worker's authority (%s; "
            "SKILL.md, Authorization boundaries) — blocked by the kraken Pi "
            "extension. Do not retry it another way; escalate if the task "
            "needs it." % (pair[0], pair[1], DENIED[pair]))


def main():
    found = denied(sys.stdin.read())
    if found:
        print(found)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
