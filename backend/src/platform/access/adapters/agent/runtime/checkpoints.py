"""Bounded conversation checkpoints persisted atomically by RunStore.

Workspace bytes belong to the provider; this manifest only contains its opaque
recovery reference. No S3 client, file inventory or Git bundle is accepted.
"""

import copy
import hashlib
import json

MAX_BYTES = 8 * 1024 * 1024


def encoded(value):
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    if len(data) > MAX_BYTES:
        raise ValueError("Conversation checkpoint exceeds limit")
    return data


class Checkpoints:
    async def save(self, run, value):
        if any(key in value for key in ("files", "base_files", "git", "modes")):
            raise ValueError("Workspace bytes cannot enter conversation checkpoints")
        data = encoded(value)
        return {
            "version": 2,
            "project_id": run["project_id"],
            "run_id": run["id"],
            "sha256": hashlib.sha256(data).hexdigest(),
            "state": copy.deepcopy(value),
        }

    async def load(self, run, manifest):
        if (
            manifest.get("version") != 2
            or manifest.get("project_id") != run["project_id"]
            or manifest.get("run_id") != run["id"]
        ):
            raise ValueError("Checkpoint version or Project binding mismatch")
        data = encoded(manifest["state"])
        if hashlib.sha256(data).hexdigest() != manifest["sha256"]:
            raise ValueError("Checkpoint checksum mismatch")
        return copy.deepcopy(manifest["state"])
