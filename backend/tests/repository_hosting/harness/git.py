"""Deterministic native Git driver. Never reads a developer's Git config/hooks."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


class Git:
    def __init__(self, path: Path):
        self.path = path

    @classmethod
    def init(cls, path: Path, *, bare=False, format="sha1"):
        path.mkdir(parents=True)
        repo = cls(path)
        repo.run(
            "init",
            "--initial-branch=main",
            f"--object-format={format}",
            *(["--bare"] if bare else []),
        )
        return repo

    def run(self, *args, input: bytes | None = None, check=True):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_TERMINAL_PROMPT="0",
            GIT_AUTHOR_NAME="Hosting Test",
            GIT_AUTHOR_EMAIL="hosting@example.test",
            GIT_COMMITTER_NAME="Hosting Test",
            GIT_COMMITTER_EMAIL="hosting@example.test",
            GIT_AUTHOR_DATE="2026-01-01T00:00:00+0000",
            GIT_COMMITTER_DATE="2026-01-01T00:00:00+0000",
            GIT_EDITOR="true",
            GIT_SEQUENCE_EDITOR="true",
            LC_ALL="C",
        )
        result = subprocess.run(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "protocol.file.allow=always",
                "-C",
                str(self.path),
                *map(str, args),
            ],
            input=input,
            capture_output=True,
            env=env,
            timeout=30,
        )
        if check and result.returncode:
            raise AssertionError(
                f"git {args}: {result.returncode}\n{result.stderr.decode(errors='replace')}"
            )
        return result

    def text(self, *args):
        return self.run(*args).stdout.decode().strip()

    def commit(self, files: dict[str, bytes | None], message="test"):
        for name, content in files.items():
            path = self.path / name
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
        self.run("add", "-A")
        self.run("commit", "--allow-empty", "-m", message)
        return self.text("rev-parse", "HEAD")

    def refs(self):
        lines = self.text("for-each-ref", "--format=%(refname) %(objectname)")
        return dict(line.split(" ", 1) for line in lines.splitlines())

    def objects(self):
        output = self.text(
            "cat-file", "--batch-all-objects", "--batch-check=%(objectname) %(objecttype)"
        )
        return {
            oid: (kind, self.run("cat-file", kind, oid).stdout)
            for oid, kind in (line.split() for line in output.splitlines())
        }
