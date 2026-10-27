"""PreToolUse guard for Claude Code.

Blocks, regardless of what the model was asked to do:
- pushing to main/master, and force pushes (changes go through PRs)
- deploy and publish commands (a human or CI does that)
- reading or writing secret files (.env, signing keys), from file tools or the shell

Exit code 2 blocks the tool call and shows the message to the model.

This is defence in depth, not a sandbox: a determined shell command can be written in ways no
pattern list anticipates. The real controls are branch protection on `main`, deploy credentials
that only CI and humans hold, and secrets that never live in the working tree.
"""

from __future__ import annotations

import json
import re
import sys

# `git` may be followed by global options: `git -C dir push`, `git -c k=v push`, `git --no-pager push`.
GIT_PUSH = r"\bgit(?:\s+-[A-Za-z]\s+\S+|\s+--?[\w-]+(?:=\S+)?)*\s+push\b"

BLOCKED_COMMANDS = [
    (re.compile(GIT_PUSH + r".*\b(main|master)\b", re.I), "push to main: open a PR instead"),
    (re.compile(GIT_PUSH + r".*(\s--force(-with-lease)?\b|\s-f\b|\s\+\S)", re.I), "force push"),
    (re.compile(r"\bterraform\s+apply\b", re.I), "terraform apply: deploys are done by a human"),
    (
        re.compile(r"\bgcloud\b.*\b(deploy|storage\s+cp)\b", re.I),
        "gcloud deploy/copy: done by a human",
    ),
    (re.compile(r"\bdocker\s+push\b", re.I), "docker push: images are published by CI"),
    (
        re.compile(r"(^|[\s<>|;&'\"=/])\.env(\.(?!example\b)[\w-]+)?(?![\w.])", re.I),
        ".env from the shell",
    ),
    (re.compile(r"\.dev-keys|\.pem\b", re.I), "signing keys from the shell"),
]
SECRET_PATHS = re.compile(r"(^|[\\/])(\.env(\.[^\\/]*)?|\.dev-keys[\\/].*|[^\\/]*\.pem)$", re.I)
FILE_TOOLS = {"Read", "Write", "Edit", "MultiEdit", "NotebookEdit"}


def check(event: dict[str, object]) -> str | None:
    tool = event.get("tool_name")
    data = event.get("tool_input") or {}
    if not isinstance(data, dict):
        return None
    if tool == "Bash":
        command = str(data.get("command", ""))
        for pattern, reason in BLOCKED_COMMANDS:
            if pattern.search(command):
                return f"Blocked: {reason}."
    if tool in FILE_TOOLS:
        path = str(data.get("file_path") or data.get("notebook_path") or "")
        if SECRET_PATHS.search(path) and not path.lower().endswith(".env.example"):
            return "Blocked: secret files are handled by a human."
    return None


def main() -> None:
    reason = check(json.load(sys.stdin))
    if reason:
        print(reason, file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
