"""Run-bound access to the existing smart-HTTP Git implementation.

The private provider relay carries HTTP bytes, not a second Git wire protocol.
Only this Git adapter knows refs, object formats or transport spools.
"""

from __future__ import annotations

import base64
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from src.version_engine.adapters.git.execution import MAX_PACK_BYTES, admission, run_owned
from src.version_engine.adapters.git.native_repository import NativeGitRepository
from src.version_engine.read.ref_metadata import GitBranchBase


@dataclass(frozen=True)
class RunPublication:
    request_key: str
    base: GitBranchBase
    candidate: str

    def validate(self, edits):
        expected = self.base.edits(self.candidate)[-1]
        if len(edits) != 1 or edits[0] != expected:
            raise PermissionError("Git push does not match the admitted run candidate")


class RunGitTransport:
    def __init__(self, manager):
        self.manager = manager

    def describe(self, project, grant):
        return GitBranchBase.from_metadata(
            self.manager.get_native_ref_metadata(project, grant)
        ).wire()

    def result(self, project, grant, request_key):
        status = self.manager.get_native_operation_status(project, grant, request_key)
        if status is None:
            return None
        return status["result"] or {"status": "pending"}

    async def exchange(self, project, grant, frame, *, base, publication=None):
        prefix = f"/git/{project}.git/"
        url = urlsplit(frame["path"])
        if url.scheme or url.netloc or url.fragment or not url.path.startswith(prefix):
            raise PermissionError("Git relay target mismatch")
        route, method = url.path[len(prefix) :], frame["method"]
        query = parse_qs(url.query, keep_blank_values=True, strict_parsing=True)
        discovery = route == "info/refs" and method == "GET"
        service_name = query.get("service", [""])[0] if discovery else route
        if discovery and (set(query) != {"service"} or len(query["service"]) != 1):
            raise PermissionError("Git discovery query denied")
        if service_name not in {"git-upload-pack", "git-receive-pack"}:
            raise PermissionError("Git relay service denied")
        if not discovery and (method != "POST" or query):
            raise PermissionError("Git relay method denied")
        if service_name == "git-receive-pack" and publication is None:
            raise PermissionError("Git publication is not admitted")
        encoded = frame.get("body", "")
        if not isinstance(encoded, str) or len(encoded) > ((MAX_PACK_BYTES + 2) // 3) * 4:
            raise ValueError("Git input exceeds limit")
        data = base64.b64decode(encoded, validate=True)
        if len(data) > MAX_PACK_BYTES:
            raise ValueError("Git input exceeds limit")
        with admission() as retain:
            selected = GitBranchBase.parse(base)
            service = self.manager.service_from_metadata(
                project,
                {
                    "project_id": project,
                    "object_format": selected.object_format,
                    "repository_profile": "native",
                },
            )
            if service is None:
                raise ValueError("Native repository unavailable")
            repo = NativeGitRepository(service, expected_generation=selected.generation)
            with tempfile.TemporaryDirectory(prefix="agent-git-http-") as directory:
                source = Path(directory) / "request"
                source.write_bytes(data)
                if discovery:
                    response = await run_owned(
                        repo.info_refs, grant, service_name, protocol=frame.get("protocol", "")
                    )
                elif service_name == "git-upload-pack":
                    response = await run_owned(
                        repo.upload, grant, source, protocol=frame.get("protocol", "")
                    )
                else:
                    response = await run_owned(repo.receive, grant, source, publication=publication)
                response = retain(response)
                body = bytearray()
                try:
                    if hasattr(response, "body_iterator"):
                        async for chunk in response.body_iterator:
                            body.extend(chunk)
                            if len(body) > MAX_PACK_BYTES:
                                raise ValueError("Git response exceeds limit")
                    else:
                        body.extend(response.body)
                    return {
                        "status": response.status_code,
                        "content_type": response.media_type,
                        "body": base64.b64encode(body).decode(),
                    }
                finally:
                    if hasattr(response, "close"):
                        await run_owned(response.close)
