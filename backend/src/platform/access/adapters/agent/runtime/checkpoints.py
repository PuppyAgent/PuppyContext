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
        if not name or name.startswith("/") or clean_path(name) != name:
            raise ValueError("Invalid workspace filename")
        size += len(base64.b64decode(value, validate=True))
        if size > MAX_BYTES:
            raise ValueError("Workspace byte limit exceeded")
    return files


class Checkpoints:
    def __init__(self, storage=None):
        self.storage = storage if storage is not None else get_s3_service()

    async def save(self, run, value):
        validate_files(value.get("files", {}))
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        if len(raw) > MAX_BYTES * 3:
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
            raw = stream.read(MAX_BYTES * 3 + 1)
        if len(raw) > MAX_BYTES * 3:
            raise ValueError("Checkpoint byte limit exceeded")
        value = json.loads(raw)
        if value.get("pi_version") != "0.85.1" or value.get("version") != 1:
            raise ValueError("Unsupported Pi checkpoint")
        validate_files(value.get("files", {}))
        return value
