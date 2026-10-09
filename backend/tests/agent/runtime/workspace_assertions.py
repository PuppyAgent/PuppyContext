"""Read actual provider recovery material, including after its container is gone."""

import subprocess

from src.config import settings

IMAGE = settings.CLOUD_AGENT_IMAGE


def recovery_command(checkpoint, *command):
    recovery = checkpoint["recovery"]
    assert recovery["provider"] == "docker"
    return subprocess.check_output(
        [
            "docker",
            "run",
            "--rm",
            "--network=none",
            "--read-only",
            "--mount",
            f"type=volume,source={recovery['volume']},target=/recovery,readonly",
            "--workdir",
            f"/recovery/{recovery['id']}",
            "--entrypoint",
            command[0],
            IMAGE,
            *command[1:],
        ],
        timeout=30,
    )


def recovery_file(checkpoint, name):
    return recovery_command(checkpoint, "cat", "--", name)


def recovery_git(checkpoint, *args):
    return recovery_command(checkpoint, "git", "-c", "safe.directory=*", *args).decode().strip()
