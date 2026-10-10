"""Automatic sandbox work with confirmation for recognizable destructive actions.

This is a UX guard, not a shell security boundary: arbitrary programs can have
effects that cannot be inferred from their command text. OS isolation, grants,
read-only enforcement and fenced Git publication remain authoritative.
"""

import posixpath
import shlex


def approval_reason(name: str, value: dict) -> str | None:
    if name in {"write", "edit"}:
        path = posixpath.normpath(str(value.get("path", "")))
        if path.startswith("/workspace/repo/"):
            path = path[len("/workspace/repo/") :]
        if path == ".git" or path.startswith(".git/"):
            return "Change Git repository metadata"
    if name == "bash":
        return _shell_reason(str(value.get("command", "")))
    return None


def _shell_reason(command: str, depth: int = 0) -> str | None:
    if depth > 8:
        return "Review deeply nested shell execution"
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        # Invalid syntax is a normal tool error, not a reason to wait for a human.
        return None
    words: list[str] = []
    for token in [*tokens, ";"]:
        if token and all(char in ";&|()\n" for char in token):
            reason = _command_reason(words, depth)
            if reason:
                return reason
            words = []
        else:
            words.append(token)
    return None


def _command_reason(words: list[str], depth: int) -> str | None:
    # Common shell prefixes. Do not search argument strings: e.g. printing a
    # document that mentions `git reset --hard` must execute without a prompt.
    while words and (
        words[0] in {"if", "then", "elif", "else", "do", "!", "command", "exec", "env"}
        or ("=" in words[0] and words[0].split("=", 1)[0].isidentifier())
    ):
        words = words[1:]
    if not words:
        return None
    executable, *args = words
    executable = posixpath.basename(executable)
    if executable in {"bash", "sh", "zsh", "dash"}:
        for index, arg in enumerate(args[:-1]):
            if arg.startswith("-") and not arg.startswith("--") and "c" in arg:
                return _shell_reason(args[index + 1], depth + 1)
    if executable == "eval":
        return _shell_reason(" ".join(args), depth + 1)
    if executable == "git":
        while args and args[0].startswith("-"):
            option = args.pop(0)
            if option in {"-C", "-c", "--git-dir", "--work-tree", "--namespace"} and args:
                args.pop(0)
        return _git_reason(args)
    if executable == "rm":
        recursive = any(_flag(arg, "rR", "--recursive") for arg in args)
        targets = [posixpath.normpath(arg) for arg in args if not arg.startswith("-")]
        if any(".git" in target.split("/") for target in targets):
            return "Delete Git repository metadata"
        roots = {".", "..", "/", "/workspace", "/workspace/repo", "~", "$PWD"}
        if any(target.endswith("*") or (recursive and target in roots) for target in targets):
            return "Recursively delete a workspace or many files"
    if executable == "find" and "-delete" in args:
        return "Delete files across a directory tree"
    return None


def _flag(value: str, short: str, *long: str) -> bool:
    return value in long or (
        value.startswith("-")
        and not value.startswith("--")
        and any(flag in value[1:] for flag in short)
    )


def _git_reason(args: list[str]) -> str | None:
    if not args:
        return None
    verb, *options = args
    if verb == "reset" and "--hard" in options:
        return "Discard uncommitted changes"
    if (
        verb == "clean"
        and not any(_flag(arg, "n", "--dry-run") for arg in options)
        and any(_flag(arg, "f", "--force") for arg in options)
    ):
        return "Delete untracked files"
    if verb == "switch" and any(
        _flag(arg, "fC", "--force", "--discard-changes", "--force-create") for arg in options
    ):
        return "Discard working changes or overwrite a branch"
    if verb in {"checkout", "restore"}:
        if "--" in options or any(_flag(arg, "fW", "--force", "--worktree") for arg in options):
            return "Discard working tree changes"
        if verb == "restore" and not any(_flag(arg, "S", "--staged") for arg in options):
            return "Discard working tree changes"
    if verb == "push" and any(_flag(arg, "n", "--dry-run") for arg in options):
        return None
    if verb == "push" and any(
        _flag(
            arg,
            "fd",
            "--force",
            "--force-with-lease",
            "--force-if-includes",
            "--mirror",
            "--delete",
        )
        or arg.startswith(("+", "--force-with-lease="))
        or (arg.startswith(":") and len(arg) > 1)
        for arg in options
    ):
        return "Rewrite or delete remote Git history"
    if verb == "branch" and any(_flag(arg, "dDf", "--delete", "--force") for arg in options):
        return "Delete or overwrite a Git branch"
    if verb in {"filter-branch", "filter-repo", "prune"} or (
        verb == "reflog" and options and options[0] in {"expire", "delete", "drop"}
    ):
        return "Rewrite or remove Git history"
    return None
