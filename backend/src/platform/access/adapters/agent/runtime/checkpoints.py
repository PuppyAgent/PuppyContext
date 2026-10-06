"""Immutable recovery objects; DB manifests are published only after PUT."""

import base64
import gzip
import hashlib
import json

from src.infra.s3.dependencies import get_s3_service
from src.platform.access.adapters.agent.runtime.admission import clean_path

MAX_BYTES = 64 * 1024 * 1024


def validate_files(files):
    if not isinstance(files, dict) or len(files) > 10000:
        raise ValueError("Workspace file limit exceeded")
    size = 0
    for name, value in files.items():
        if (
            not name
            or name.startswith("/")
            or clean_path(name) != name
            or any(part.lower() == ".git" for part in name.split("/"))
        ):
            raise ValueError("Invalid workspace filename")
        size += len(base64.b64decode(value, validate=True))
        if size > MAX_BYTES:
            raise ValueError("Workspace byte limit exceeded")
    return files


def validate_workspace(value):
    if not isinstance(value, dict) or "files" not in value:
        raise ValueError("Missing workspace checkpoint files")
    validate_files(value.get("files", {}))
    modes = value.get("modes", {})
    if not isinstance(modes, dict) or any(
        name not in value.get("files", {}) or mode not in {"100644", "100755"}
        for name, mode in modes.items()
    ):
        raise ValueError("Invalid workspace modes")
    if "git" in value:
        from src.platform.access.adapters.agent.runtime.git_workspace import MAX_GIT_BYTES

        state = value["git"]
        if not isinstance(state, dict) or state.get("object_format") not in {"sha1", "sha256"}:
            raise ValueError("Invalid Git checkpoint")
        for key, limit in (("bundle", MAX_GIT_BYTES), ("index", 4 * 1024 * 1024), ("head", 1024)):
            encoded = state.get(key)
            if encoded is not None and (
                not isinstance(encoded, str)
                or len(encoded) > (limit + 2) // 3 * 4
                or len(base64.b64decode(encoded, validate=True)) > limit
            ):
                raise ValueError("Invalid Git checkpoint size")
        tip = state.get("tip")
        if tip is not None and (
            not isinstance(tip, str)
            or len(tip) != (40 if state["object_format"] == "sha1" else 64)
            or not set(tip) <= set("0123456789abcdef")
        ):
            raise ValueError("Invalid Git checkpoint tip")
    return value


class Checkpoints:
    def __init__(self, storage=None):
        self.storage = storage if storage is not None else get_s3_service()

    async def save(self, run, value):
        validate_workspace(value)
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        if len(raw) > MAX_BYTES * 4:
            raise ValueError("Checkpoint byte limit exceeded")
        data = gzip.compress(raw, mtime=0)
        checksum = hashlib.sha256(data).hexdigest()
        key = f"projects/{run['project_id']}/agent-runs/{run['id']}/{checksum}.json.gz"
        await self.storage.upload_file(key, data, content_type="application/gzip")
        return {
            "key": key,
            "sha256": checksum,
            "size": len(data),
            "version": 1,
            "pi_version": "0.85.1",
        }

    async def load(self, run, manifest):
        prefix = f"projects/{run['project_id']}/agent-runs/{run['id']}/"
        if not manifest["key"].startswith(prefix) or ".." in manifest["key"]:
            raise ValueError("Checkpoint Project binding mismatch")
        data = await self.storage.download_file(manifest["key"])
        if len(data) != manifest["size"] or hashlib.sha256(data).hexdigest() != manifest["sha256"]:
            raise ValueError("Checkpoint checksum mismatch")
        import io

        with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
            raw = stream.read(MAX_BYTES * 4 + 1)
        if len(raw) > MAX_BYTES * 4:
            raise ValueError("Checkpoint byte limit exceeded")
        value = json.loads(raw)
        if value.get("pi_version") != "0.85.1" or value.get("version") != 1:
            raise ValueError("Unsupported Pi checkpoint")
        validate_workspace(value)
        return value
