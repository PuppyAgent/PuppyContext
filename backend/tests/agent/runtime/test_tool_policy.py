"""Cloud sandbox approval UX: ordinary work proceeds, destructive commands wait."""

import pytest

from src.platform.access.adapters.agent.runtime.tool_policy import approval_reason


@pytest.mark.parametrize(
    "command",
    [
        "ls -la; find . -type f | wc -l",
        "cd /workspace/repo && git log --oneline -15 2>/dev/null; git status | head -40",
        "cat /etc/os-release; printf ok >/dev/null",
        "mkdir -p notes; printf hello > notes/a.md; mv notes/a.md notes/b.md",
        "rm notes/obsolete.md; rm -rf build/cache",
        "rm -rf notes/.github",
        "git add . && git commit -m 'Saved' && git push origin HEAD",
        "git checkout -b draft; git branch --list; git restore --staged notes.md",
        "git clean -ndf; git clean --dry-run --force",
        "git push --dry-run --force origin main",
        "echo 'git reset --hard; rm -rf /workspace/repo'",
        "printf '%s' 'git push --force' > commands.txt",
        "# git reset --hard\ngit status",
        "python -c 'print(42)'; node -e 'console.log(42)'",
        "bash -lc 'git status 2>/dev/null'",
        "printf 'synthetic failure' >&2; exit 7",
        "echo 'invalid shell",
    ],
)
def test_ordinary_commands_do_not_require_approval(command):
    assert approval_reason("bash", {"command": command}) is None


@pytest.mark.parametrize(
    "command",
    [
        "git reset --hard HEAD",
        "git -C /workspace/repo reset --hard",
        "git --git-dir=/workspace/repo/.git reset --hard",
        "git status && git reset --hard",
        "git status\ngit reset --hard",
        "FOO=bar /usr/bin/git reset --hard",
        "env FOO=bar git reset --hard",
        "if true; then git reset --hard; fi",
        "bash -lc 'git reset --hard'",
        "eval 'git reset --hard'",
        "git clean -fdx",
        "git checkout -- notes.md",
        "git checkout -f main",
        "git switch --discard-changes main",
        "git restore notes.md",
        "git restore --staged --worktree notes.md",
        "git push -f origin main",
        "git push origin +HEAD:main",
        "git push --force-with-lease=main:abc origin HEAD",
        "git push --delete origin main",
        "git push origin :main",
        "git branch -D main",
        "git filter-repo --path notes.md --invert-paths",
        "git reflog expire --expire=now --all; git prune",
        "rm -rf /workspace/repo",
        "rm -rf /workspace/repo/*",
        "rm -rf ./*",
        "rm -f *",
        "rm -r .",
        "rm -rf .git",
        "rm -rf /workspace/repo/.git",
        "rm .git/config",
        "find . -type f -delete",
    ],
)
def test_destructive_commands_require_approval(command):
    assert approval_reason("bash", {"command": command})


@pytest.mark.parametrize("name", ["read", "ls", "find", "grep", "write", "edit"])
def test_normal_file_tools_execute_automatically(name):
    assert approval_reason(name, {"path": "notes.md", "content": "git reset --hard"}) is None


@pytest.mark.parametrize("name", ["write", "edit"])
@pytest.mark.parametrize("path", [".git/config", "/workspace/repo/.git/HEAD", "./.git/index"])
def test_git_metadata_edits_require_confirmation(name, path):
    assert approval_reason(name, {"path": path})


def test_reading_git_metadata_does_not_require_confirmation():
    assert approval_reason("read", {"path": ".git/config"}) is None
