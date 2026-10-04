"""Version write command normalization.

This module is an ingress-adapter normalization helper in the Version Engine
architecture. Protocol routes own auth, permission, and response shape; the
command service owns syntactic path cleanup, content serialization, default
command metadata, and dispatch into the Product Operation Adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
import json as _json
from typing import Any

from src.version_engine.adapters.product.operation_adapter import (
    BlobRef,
    ProductOperationAdapter,
    WriteResult,
)
from src.version_engine.domain.intents import ProjectWriteState
from src.version_engine.admission.validation import validate_content_size, validate_path

_EXT_MAP = {"json": ".json", "markdown": ".md"}


@dataclass(frozen=True)
class SerializedContent:
    path: str
    content: bytes
    node_type: str

    @property
    def size_bytes(self) -> int:
        return len(self.content)


@dataclass(frozen=True)
class WriteCommandOutcome:
    result: WriteResult
    path: str = ""
    paths: list[str] | None = None
    old_path: str = ""
    new_path: str = ""
    size_bytes: int = 0


class VersionWriteCommandService:
    """Build and dispatch typed write commands.

    Keep this layer protocol-neutral. It should not know how Product auth,
    Access Point credentials, Git receive-pack, or internal secrets work.
    """

    def __init__(self, ops: ProductOperationAdapter):
        self._ops = ops

    @property
    def ops(self) -> ProductOperationAdapter:
        return self._ops

    @staticmethod
    def normalize_path(path: str) -> str:
        return validate_path(path)

    @staticmethod
    def normalize_paths(paths: list[str]) -> list[str]:
        return [validate_path(p) for p in paths if p]

    @staticmethod
    def serialize_content(path: str, content: Any, node_type: str, *, path_validator=validate_path) -> SerializedContent:
        """Convert request content to Git blob bytes and canonicalize extension."""

        clean_path = path_validator(path)
        if node_type == "json":
            if isinstance(content, str):
                data = content.encode("utf-8")
            else:
                data = _json.dumps(
                    content,
                    ensure_ascii=False,
                    indent=2,
                ).encode("utf-8")
        elif node_type == "markdown":
            data = (
                content if isinstance(content, str) else str(content)
            ).encode("utf-8")
        elif isinstance(content, bytes):
            data = content
        elif isinstance(content, str):
            data = content.encode("utf-8")
        else:
            data = _json.dumps(content, ensure_ascii=False).encode("utf-8")

        ext = _EXT_MAP.get(node_type)
        if ext and not clean_path.endswith(ext):
            clean_path += ext
        validate_content_size(data)
        return SerializedContent(
            path=clean_path,
            content=data,
            node_type=node_type,
        )

    @staticmethod
    def validate_bytes(content: bytes) -> bytes:
        validate_content_size(content)
        return content

    async def native_operation(self, project_id: str, grant, *, operation: str,
                               arguments: dict, request_key: str, base: dict, byte_paths: dict | None = None):
        from src.version_engine.adapters.product.native_commands import (
            apply_byte_paths,
            compile_native_command,
            response_paths,
        )
        from src.version_engine.write_engine.errors import NativeRevisionConflictError

        arguments = apply_byte_paths(arguments, byte_paths)
        supplied = arguments.get("base_commit_id")
        if supplied is not None and supplied != (base.get("expected_oid") or ""):
            raise NativeRevisionConflictError("Product base and native starting revision differ")
        digest, splice, response = compile_native_command(self, operation, arguments)
        result = await self._ops.apply_native_command(
            project_id, grant, request_key=request_key, base=base, input_sha256=digest,
            splice=splice, message=arguments.get("message") or operation,
        )
        if result["status"] != "committed":
            raise NativeRevisionConflictError("native starting ref or HEAD changed")
        return {**response_paths(response), "commit_id": result["product"]["commit_oid"] or "",
                "repository_transaction": result}

    def _operation_kwargs(
        self,
        *,
        actor: str,
        message: str,
        scope: str = "",
        base_commit_id: str | None = None,
        defer_projection: bool = False,
        policy: str = "",
        source_channel: str = "papi",
        project_write_state: ProjectWriteState | None = None,
        pusher_client_id: str = "",
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"who": actor}
        if message:
            kwargs["message"] = message
        if scope:
            kwargs["scope"] = scope
        if base_commit_id is not None:
            kwargs["base_commit_id"] = base_commit_id
        if defer_projection:
            kwargs["defer_projection"] = True
        if policy:
            kwargs["policy"] = policy
        if source_channel != "papi":
            kwargs["source_channel"] = source_channel
        if project_write_state is not None:
            kwargs["project_write_state"] = project_write_state
        if pusher_client_id:
            kwargs["pusher_client_id"] = pusher_client_id
        return kwargs

    async def write_file(
        self,
        project_id: str,
        path: str,
        content: Any,
        *,
        node_type: str = "file",
        actor: str,
        scope: str = "",
        message: str = "",
        default_message_prefix: str = "write",
        base_commit_id: str | None = None,
        defer_projection: bool = False,
        policy: str = "",
        source_channel: str = "papi",
        project_write_state: ProjectWriteState | None = None,
        pusher_client_id: str = "",
    ) -> WriteCommandOutcome:
        serialized = self.serialize_content(path, content, node_type)
        command_message = message or f"{default_message_prefix} {serialized.path}"
        result = await self._ops.write_file(
            project_id,
            serialized.path,
            serialized.content,
            **self._operation_kwargs(
                actor=actor,
                message=command_message,
                scope=scope,
                base_commit_id=base_commit_id,
                defer_projection=defer_projection,
                policy=policy,
                source_channel=source_channel,
                project_write_state=project_write_state,
                pusher_client_id=pusher_client_id,
            ),
        )
        return WriteCommandOutcome(
            result=result,
            path=serialized.path,
            paths=[serialized.path],
            size_bytes=serialized.size_bytes,
        )

    async def write_bytes(
        self,
        project_id: str,
        path: str,
        content: bytes,
        *,
        actor: str,
        scope: str = "",
        message: str = "",
        default_message_prefix: str = "write",
        base_commit_id: str | None = None,
        defer_projection: bool = False,
        policy: str = "",
        source_channel: str = "papi",
        project_write_state: ProjectWriteState | None = None,
    ) -> WriteCommandOutcome:
        clean_path = validate_path(path)
        self.validate_bytes(content)
        command_message = message or f"{default_message_prefix} {clean_path}"
        result = await self._ops.write_file(
            project_id,
            clean_path,
            content,
            **self._operation_kwargs(
                actor=actor,
                message=command_message,
                scope=scope,
                base_commit_id=base_commit_id,
                defer_projection=defer_projection,
                policy=policy,
                source_channel=source_channel,
                project_write_state=project_write_state,
            ),
        )
        return WriteCommandOutcome(
            result=result,
            path=clean_path,
            paths=[clean_path],
            size_bytes=len(content),
        )

    async def mkdir(
        self,
        project_id: str,
        path: str,
        *,
        actor: str,
        scope: str = "",
        message: str = "",
        default_message_prefix: str = "mkdir",
        base_commit_id: str | None = None,
        defer_projection: bool = False,
        policy: str = "",
        source_channel: str = "papi",
        project_write_state: ProjectWriteState | None = None,
    ) -> WriteCommandOutcome:
        clean_path = validate_path(path)
        command_message = message or f"{default_message_prefix} {clean_path}"
        result = await self._ops.mkdir(
            project_id,
            clean_path,
            **self._operation_kwargs(
                actor=actor,
                message=command_message,
                scope=scope,
                base_commit_id=base_commit_id,
                defer_projection=defer_projection,
                policy=policy,
                source_channel=source_channel,
                project_write_state=project_write_state,
            ),
        )
        return WriteCommandOutcome(result=result, path=clean_path, paths=[clean_path])

    async def move(
        self,
        project_id: str,
        old_path: str,
        new_path: str,
        *,
        actor: str,
        scope: str = "",
        message: str = "",
        default_message_prefix: str = "move",
        base_commit_id: str | None = None,
        defer_projection: bool = False,
        policy: str = "",
        source_channel: str = "papi",
        project_write_state: ProjectWriteState | None = None,
    ) -> WriteCommandOutcome:
        old_clean = validate_path(old_path)
        new_clean = validate_path(new_path)
        command_message = message or f"{default_message_prefix} {old_clean} -> {new_clean}"
        result = await self._ops.move(
            project_id,
            old_clean,
            new_clean,
            **self._operation_kwargs(
                actor=actor,
                message=command_message,
                scope=scope,
                base_commit_id=base_commit_id,
                defer_projection=defer_projection,
                policy=policy,
                source_channel=source_channel,
                project_write_state=project_write_state,
            ),
        )
        return WriteCommandOutcome(
            result=result,
            old_path=old_clean,
            new_path=new_clean,
            paths=[old_clean, new_clean],
        )

    async def copy(
        self,
        project_id: str,
        old_path: str,
        new_path: str,
        *,
        actor: str,
        scope: str = "",
        message: str = "",
        default_message_prefix: str = "copy",
        base_commit_id: str | None = None,
        defer_projection: bool = False,
        policy: str = "",
        source_channel: str = "papi",
        project_write_state: ProjectWriteState | None = None,
    ) -> WriteCommandOutcome:
        old_clean = validate_path(old_path)
        new_clean = validate_path(new_path)
        command_message = message or f"{default_message_prefix} {old_clean} -> {new_clean}"
        result = await self._ops.copy(
            project_id,
            old_clean,
            new_clean,
            **self._operation_kwargs(
                actor=actor,
                message=command_message,
                scope=scope,
                base_commit_id=base_commit_id,
                defer_projection=defer_projection,
                policy=policy,
                source_channel=source_channel,
                project_write_state=project_write_state,
            ),
        )
        return WriteCommandOutcome(
            result=result,
            old_path=old_clean,
            new_path=new_clean,
            paths=[old_clean, new_clean],
        )

    async def touch(
        self,
        project_id: str,
        paths: list[str],
        *,
        actor: str,
        scope: str = "",
        message: str = "",
        default_message_prefix: str = "touch",
        base_commit_id: str | None = None,
        defer_projection: bool = False,
        policy: str = "",
        source_channel: str = "papi",
        project_write_state: ProjectWriteState | None = None,
    ) -> WriteCommandOutcome:
        clean = self.normalize_paths(paths)
        command_message = message or (
            f"{default_message_prefix} {clean[0]}"
            if len(clean) == 1
            else f"{default_message_prefix} {len(clean)} files"
        )
        result = await self._ops.touch(
            project_id,
            clean,
            **self._operation_kwargs(
                actor=actor,
                message=command_message,
                scope=scope,
                base_commit_id=base_commit_id,
                defer_projection=defer_projection,
                policy=policy,
                source_channel=source_channel,
                project_write_state=project_write_state,
            ),
        )
        return WriteCommandOutcome(result=result, paths=clean)

    async def delete(
        self,
        project_id: str,
        paths: list[str],
        *,
        actor: str,
        scope: str = "",
        message: str = "",
        default_message_prefix: str = "delete",
        base_commit_id: str | None = None,
        defer_projection: bool = False,
        policy: str = "",
        source_channel: str = "papi",
        project_write_state: ProjectWriteState | None = None,
    ) -> WriteCommandOutcome:
        clean = self.normalize_paths(paths)
        command_message = message or (
            f"{default_message_prefix} {clean[0]}"
            if len(clean) == 1
            else f"{default_message_prefix} {len(clean)} paths"
        )
        result = await self._ops.delete(
            project_id,
            clean,
            **self._operation_kwargs(
                actor=actor,
                message=command_message,
                scope=scope,
                base_commit_id=base_commit_id,
                defer_projection=defer_projection,
                policy=policy,
                source_channel=source_channel,
                project_write_state=project_write_state,
            ),
        )
        return WriteCommandOutcome(result=result, paths=clean)

    async def bulk_write(
        self,
        project_id: str,
        files: dict[str, Any],
        *,
        actor: str,
        node_types: dict[str, str] | None = None,
        scope: str = "",
        deleted: list[str] | None = None,
        message: str = "",
        default_message: str = "bulk write",
        defer_projection: bool = False,
        policy: str = "",
        source_channel: str = "papi",
        project_write_state: ProjectWriteState | None = None,
        base_commit_id: str | None = None,
    ) -> WriteCommandOutcome:
        modified: dict[str, bytes] = {}
        for path, content in files.items():
            node_type = (node_types or {}).get(path, "file")
            serialized = self.serialize_content(path, content, node_type)
            modified[serialized.path] = serialized.content

        clean_deleted = self.normalize_paths(deleted or [])
        result = await self._ops.bulk_write(
            project_id,
            modified,
            deleted=clean_deleted,
            **self._operation_kwargs(
                actor=actor,
                message=message or default_message,
                scope=scope,
                base_commit_id=base_commit_id,
                defer_projection=defer_projection,
                policy=policy,
                source_channel=source_channel,
                project_write_state=project_write_state,
            ),
        )
        return WriteCommandOutcome(
            result=result,
            paths=list(modified.keys()) + clean_deleted,
        )

    async def bulk_write_refs(
        self,
        project_id: str,
        file_refs: dict[str, BlobRef],
        *,
        actor: str,
        scope: str = "",
        deleted: list[str] | None = None,
        message: str = "",
        verify_blobs: bool = True,
        source_channel: str = "papi",
        project_write_state: ProjectWriteState | None = None,
        base_commit_id: str | None = None,
    ) -> WriteCommandOutcome:
        clean = {validate_path(path): ref for path, ref in file_refs.items()}
        clean_deleted = self.normalize_paths(deleted or [])
        result = await self._ops.bulk_write_refs(
            project_id=project_id,
            file_refs=clean,
            who=actor,
            scope=scope,
            deleted=clean_deleted,
            message=message or f"bulk write {len(clean)} refs",
            verify_blobs=verify_blobs,
            source_channel=source_channel,
            project_write_state=project_write_state,
            **({"base_commit_id": base_commit_id} if base_commit_id is not None else {}),
        )
        return WriteCommandOutcome(
            result=result,
            paths=list(clean.keys()) + clean_deleted,
        )
