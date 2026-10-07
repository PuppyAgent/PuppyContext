from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from src.exceptions import ErrorCode, NotFoundException
from src.infra.chunking.config import ChunkingConfig
from src.infra.chunking.repository import ChunkRepository, ensure_chunks_for_pointer
from src.infra.chunking.schemas import Chunk
from src.infra.chunking.service import ChunkingService, iter_large_string_nodes_for_chunking
from src.infra.llm.embedding_service import EmbeddingService
from src.infra.s3.service import S3Service
from src.infra.turbopuffer.schemas import TurbopufferRow
from src.infra.turbopuffer.service import TurbopufferSearchService
from src.platform.authorization.models import ProjectAction
from src.platform.authorization.service import AuthorizationService
from src.platform.project.write_lease import ProjectWriteLease, ProjectWriteLeaseFactory
from src.utils.logger import log_error, log_info
from src.version_engine.adapters.product.operation_adapter import ProductOperationAdapter
from src.version_engine.read.tree_reader import VersionEntry


def _normalize_json_pointer(pointer: str) -> str:
    p = (pointer or "").strip()
    if not p:
        return ""
    if not p.startswith("/"):
        # Don't force an error here for compatibility; internally we use RFC6901 absolute pointers
        p = "/" + p
    return p


def _extract_by_pointer(data: Any, pointer: str) -> Any:
    """
    Extract sub-data at the specified JSON Pointer path from JSON data.
    """
    if not pointer or pointer == "/":
        return data

    segments = [s for s in pointer.split("/") if s]
    current = data
    for seg in segments:
        if current is None:
            return None
        if isinstance(current, list):
            try:
                idx = int(seg)
                if 0 <= idx < len(current):
                    current = current[idx]
                else:
                    return None
            except ValueError:
                return None
        elif isinstance(current, dict):
            current = current.get(seg)
        else:
            return None
    return current


def _relative_pointer(*, base: str, absolute: str) -> str:
    """
    Convert an absolute json_pointer to a path relative to base (both are RFC6901).

    Examples:
    - base="/articles", absolute="/articles/0/content" -> "/0/content"
    - base="", absolute="/a/b" -> "/a/b"
    """
    b = _normalize_json_pointer(base)
    a = _normalize_json_pointer(absolute)
    if not b:
        return a
    if a == b:
        return ""
    prefix = b + "/"
    if a.startswith(prefix):
        return a[len(b) :]
    # Not within scope: keep returning absolute path (better for debugging; avoids confusion)
    return a


def reciprocal_rank_fusion(
    result_lists: Iterable[Iterable[TurbopufferRow]], *, k: int = 60
) -> list[tuple[TurbopufferRow, float]]:
    """
    RRF (Reciprocal Rank Fusion)
    - score(doc) = Σ 1 / (k + rank)
    """
    if k <= 0:
        raise ValueError("k must be > 0")

    scores: dict[int | str, float] = {}
    items: dict[int | str, TurbopufferRow] = {}

    for results in result_lists:
        for rank, row in enumerate(results, start=1):
            doc_id = row.id
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
            # Keep one copy of the row (attributes are consistent; row attributes from different queries are usually the same)
            items[doc_id] = row

    # Sort by fused score (high to low)
    return [
        (items[doc_id], score)
        for doc_id, score in sorted(scores.items(), key=lambda x: x[1], reverse=True)
    ]


@dataclass(frozen=True)
class SearchIndexStats:
    nodes_count: int
    chunks_count: int
    indexed_chunks_count: int


@dataclass(frozen=True)
class FolderIndexStats:
    """Folder search indexing statistics"""

    total_files: int
    indexed_files: int
    nodes_count: int  # Total large string nodes across all files
    chunks_count: int  # Total chunks created
    indexed_chunks_count: int  # Total chunks indexed to turbopuffer


class SearchService:
    """
    Search Tool core capabilities:
    - index_tool: (path, json_path) scope -> chunking -> embedding -> turbopuffer upsert
    - search_tool: ANN -> structured output (chunk_text backfilled from DB)
    """

    def __init__(
        self,
        *,
        ops: ProductOperationAdapter,
        chunk_repo: ChunkRepository,
        authorization: AuthorizationService,
        chunking_service: ChunkingService | None = None,
        chunking_config: ChunkingConfig | None = None,
        embedding_service: EmbeddingService | None = None,
        turbopuffer_service: TurbopufferSearchService | None = None,
        write_lease_factory: ProjectWriteLeaseFactory = ProjectWriteLease,
    ) -> None:
        self._ops = ops
        self._chunk_repo = chunk_repo
        self.authorization = authorization
        self._chunking_service = chunking_service or ChunkingService()
        self._chunking_config = chunking_config or ChunkingConfig()
        self._embedding = embedding_service or EmbeddingService()
        self._tp = turbopuffer_service or TurbopufferSearchService()
        self._write_lease_factory = write_lease_factory

    def _ensure_project_access(self, *, project_id: str, user_id: str) -> None:
        if not self.authorization.allows(project_id, user_id, ProjectAction.CONTENT_READ):
            raise NotFoundException(
                f"Project not found: {project_id}",
                code=ErrorCode.NOT_FOUND,
            )

    @staticmethod
    def build_namespace(*, project_id: str, path: str) -> str:
        return f"project_{project_id}_path_{path}"

    @staticmethod
    def build_folder_namespace(*, project_id: str, folder_path: str) -> str:
        """Build namespace for folder search"""
        return f"project_{project_id}_folder_{folder_path}"

    @staticmethod
    def build_doc_id(*, path: str, json_pointer: str, content_hash: str, chunk_index: int) -> str:
        pointer_hash = hashlib.md5(json_pointer.encode("utf-8")).hexdigest()[:12]
        return f"{path[:12]}_{pointer_hash}_{content_hash[:8]}_{chunk_index}"

    @staticmethod
    def build_folder_doc_id(
        *, file_path: str, json_pointer: str, content_hash: str, chunk_index: int
    ) -> str:
        """
        Build doc_id for folder search.
        Similar to build_doc_id but uses file_path to distinguish files.
        """
        pointer_hash = hashlib.md5(json_pointer.encode("utf-8")).hexdigest()[:12]
        return f"{file_path[:12]}_{pointer_hash}_{content_hash[:8]}_{chunk_index}"

    async def index_scope(
        self,
        *,
        project_id: str,
        path: str,
        user_id: str,
        json_path: str,
    ) -> SearchIndexStats:
        async with self._write_lease_factory(project_id, "search.index_scope"):
            return await self._index_scope_admitted(
                project_id=project_id,
                path=path,
                user_id=user_id,
                json_path=json_path,
            )

    async def _index_scope_admitted(
        self,
        *,
        project_id: str,
        path: str,
        user_id: str,
        json_path: str,
    ) -> SearchIndexStats:
        """
        Read scope data from (path, json_path) and perform indexing.
        """
        t0 = time.perf_counter()
        scope_pointer = _normalize_json_pointer(json_path)
        self._ensure_project_access(project_id=project_id, user_id=user_id)
        log_info(
            f"[index_scope] start: project_id={project_id} path={path} json_path='{json_path}'"
        )

        # 1) Read scope data (from version ObjectStore)
        t1 = time.perf_counter()
        content_bytes = await asyncio.to_thread(
            self._ops.for_user(project_id, user_id).read_file, project_id, path
        )
        import json as _json_mod

        try:
            full_data = _json_mod.loads(content_bytes.decode("utf-8"))
        except Exception:
            full_data = {}
        scope_data = _extract_by_pointer(full_data, scope_pointer)
        log_info(
            f"[index_scope] step1_get_scope_data: path={path} elapsed_ms={int((time.perf_counter() - t1) * 1000)}"
        )

        # 2) Extract large string nodes (json_pointer must be "absolute pointer")
        t2 = time.perf_counter()
        nodes = await asyncio.to_thread(
            lambda: list(
                iter_large_string_nodes_for_chunking(
                    self._chunking_service,
                    scope_data,
                    self._chunking_config,
                    base_pointer=scope_pointer,
                )
            )
        )
        log_info(
            f"[index_scope] step2_extract_nodes: path={path} nodes_count={len(nodes)} elapsed_ms={int((time.perf_counter() - t2) * 1000)}"
        )

        if not nodes:
            log_info(
                f"[index_scope] done_no_nodes: path={path} total_ms={int((time.perf_counter() - t0) * 1000)}"
            )
            return SearchIndexStats(nodes_count=0, chunks_count=0, indexed_chunks_count=0)

        # 3) Ensure chunks (idempotent)
        t3 = time.perf_counter()
        all_chunks: list[Chunk] = []
        for i, n in enumerate(nodes):
            ensured = await asyncio.to_thread(
                ensure_chunks_for_pointer,
                repo=self._chunk_repo,
                service=self._chunking_service,
                path=path,
                json_pointer=n.json_pointer,
                content=n.content,
                config=self._chunking_config,
            )
            all_chunks.extend(list(ensured.chunks))
            if (i + 1) % 10 == 0:
                log_info(
                    f"[index_scope] step3_chunking_progress: path={path} processed={i + 1}/{len(nodes)}"
                )
        log_info(
            f"[index_scope] step3_ensure_chunks: path={path} chunks_count={len(all_chunks)} elapsed_ms={int((time.perf_counter() - t3) * 1000)}"
        )

        if not all_chunks:
            log_info(
                f"[index_scope] done_no_chunks: path={path} nodes={len(nodes)} total_ms={int((time.perf_counter() - t0) * 1000)}"
            )
            return SearchIndexStats(nodes_count=len(nodes), chunks_count=0, indexed_chunks_count=0)

        # 4) Embedding (batch)
        t4 = time.perf_counter()
        texts = [c.chunk_text for c in all_chunks]
        log_info(f"[index_scope] step4_embedding_start: path={path} texts_count={len(texts)}")
        vectors = await self._embedding.generate_embeddings_batch(texts)
        log_info(
            f"[index_scope] step4_embedding_done: path={path} vectors_count={len(vectors)} elapsed_ms={int((time.perf_counter() - t4) * 1000)}"
        )

        # 5) Turbopuffer upsert (batch)
        # Note: write with schema parameter auto-creates namespace and supports incremental updates
        t5 = time.perf_counter()
        namespace = self.build_namespace(project_id=project_id, path=path)

        upsert_rows: list[dict[str, Any]] = []
        doc_ids: list[str] = []
        for c, vec in zip(all_chunks, vectors, strict=True):
            doc_id = self.build_doc_id(
                path=c.path,
                json_pointer=c.json_pointer,
                content_hash=c.content_hash,
                chunk_index=c.chunk_index,
            )
            doc_ids.append(doc_id)
            upsert_rows.append(
                {
                    "id": doc_id,
                    "vector": vec,
                    # metadata (for backfilling chunk_text and locating)
                    "json_pointer": c.json_pointer,
                    "chunk_index": c.chunk_index,
                    "total_chunks": c.total_chunks,
                    "char_start": c.char_start,
                    "char_end": c.char_end,
                    "content_hash": c.content_hash,
                    "chunk_id": int(c.id),
                }
            )

        log_info(
            f"[index_scope] step5_turbopuffer_write_start: path={path} rows_count={len(upsert_rows)}"
        )
        async with self._write_lease_factory(project_id, "search.index_scope"):
            await self._tp.write(
                namespace,
                upsert_rows=upsert_rows,
                distance_metric="cosine_distance",
            )
        log_info(
            f"[index_scope] step5_turbopuffer_done: path={path} elapsed_ms={int((time.perf_counter() - t5) * 1000)}"
        )

        # 6) Write back turbopuffer fields to chunks table (best-effort)
        t6 = time.perf_counter()
        for c, doc_id in zip(all_chunks, doc_ids, strict=True):
            try:  # noqa: SIM105 -- Keep the best-effort indexing boundary explicit.
                await asyncio.to_thread(
                    lambda cid=int(c.id), did=doc_id: (
                        self._chunk_repo._client.table("chunks")
                        .update(
                            {
                                "turbopuffer_namespace": namespace,
                                "turbopuffer_doc_id": did,
                            }
                        )
                        .eq("id", cid)
                        .execute()
                    )
                )
            except Exception:
                # Don't block indexing: can be fixed later via rebuild/backfill logic
                pass
        log_info(
            f"[index_scope] step6_update_chunks_done: path={path} elapsed_ms={int((time.perf_counter() - t6) * 1000)}"
        )

        log_info(
            f"[index_scope] done: path={path} nodes={len(nodes)} chunks={len(all_chunks)} total_ms={int((time.perf_counter() - t0) * 1000)}"
        )
        return SearchIndexStats(
            nodes_count=len(nodes),
            chunks_count=len(all_chunks),
            indexed_chunks_count=len(all_chunks),
        )

    async def search_scope(
        self,
        *,
        project_id: str,
        path: str,
        tool_json_path: str,
        query: str,
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        """
        Execute ANN vector search on namespace, returning rows (chunk_text backfilled from DB).
        """
        q = (query or "").strip()
        if not q:
            raise ValueError("query must be non-empty")

        top_k = int(top_k)
        if top_k <= 0:
            raise ValueError("top_k must be > 0")
        if top_k > 20:
            top_k = 20

        namespace = self.build_namespace(project_id=project_id, path=path)

        query_vec = await self._embedding.generate_embedding(q)
        resp = await self._tp.query(
            namespace,
            rank_by=("vector", "ANN", query_vec),
            top_k=top_k,
            include_attributes=True,
        )
        rows = resp.rows or []
        scope_base = _normalize_json_pointer(tool_json_path)

        # Batch backfill chunk_text (avoid storing redundant large fields in turbopuffer)
        chunk_ids: list[int] = []
        for r in rows[:top_k]:
            attrs = r.attributes or {}
            cid = attrs.get("chunk_id")
            try:
                if cid is not None:
                    chunk_ids.append(int(cid))
            except (TypeError, ValueError):
                pass
        chunks = await asyncio.to_thread(self._chunk_repo.get_by_ids, chunk_ids)
        chunk_text_by_id = {int(c.id): c.chunk_text for c in chunks}

        out: list[dict[str, Any]] = []
        for r in rows[:top_k]:
            attrs = r.attributes or {}
            json_pointer = str(attrs.get("json_pointer") or "")
            json_pointer = _normalize_json_pointer(json_pointer)
            json_path = _relative_pointer(base=scope_base, absolute=json_pointer)

            # Only return fields useful for the Agent
            # Remove internal fields: path, content_hash, turbopuffer_namespace, turbopuffer_doc_id, char_start, char_end
            cid = attrs.get("chunk_id")
            chunk_id_int: int | None = None
            try:
                if cid is not None:
                    chunk_id_int = int(cid)
            except (TypeError, ValueError):
                chunk_id_int = None

            # Prefer score; otherwise construct a monotonic score from distance (closer = higher)
            score = None
            if r.score is not None:
                score = float(r.score)
            elif r.distance is not None:
                try:
                    dist = float(r.distance)
                    score = 1.0 / (1.0 + dist)
                except (TypeError, ValueError):
                    score = 0.0
            else:
                score = 0.0

            out.append(
                {
                    "score": float(score),
                    "json_path": json_path,
                    "chunk": {
                        "id": chunk_id_int,
                        "json_pointer": json_pointer,
                        "chunk_index": int(attrs.get("chunk_index") or 0),
                        "total_chunks": int(attrs.get("total_chunks") or 0),
                        "chunk_text": (
                            chunk_text_by_id.get(chunk_id_int, "")
                            if chunk_id_int is not None
                            else ""
                        ),
                    },
                }
            )

        return out

    # ==================== Folder Search Methods ====================

    async def index_folder(
        self,
        *,
        project_id: str,
        folder_path: str,
        user_id: str,
        s3_service: S3Service,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> FolderIndexStats:
        async with self._write_lease_factory(project_id, "search.index_folder"):
            return await self._index_folder_admitted(
                project_id=project_id,
                folder_path=folder_path,
                user_id=user_id,
                s3_service=s3_service,
                progress_callback=progress_callback,
            )

    async def _index_folder_admitted(
        self,
        *,
        project_id: str,
        folder_path: str,
        user_id: str,
        s3_service: S3Service,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> FolderIndexStats:
        """
        Index all indexable files in a folder for vector search.
        """
        t0 = time.perf_counter()
        self._ensure_project_access(project_id=project_id, user_id=user_id)
        log_info(f"[index_folder] start: project_id={project_id} folder_path={folder_path}")

        # 1) Get all indexable descendants
        t1 = time.perf_counter()
        all_entries = await asyncio.to_thread(
            self._ops.for_user(project_id, user_id).list_tree, project_id, folder_path
        )
        indexable_files = [e for e in all_entries if e.type in ("json", "markdown")]
        total_files = len(indexable_files)
        log_info(
            f"[index_folder] step1_get_indexable_files: folder_path={folder_path} "
            f"total_files={total_files} elapsed_ms={int((time.perf_counter() - t1) * 1000)}"
        )

        if not indexable_files:
            log_info(
                f"[index_folder] done_no_files: folder_path={folder_path} "
                f"total_ms={int((time.perf_counter() - t0) * 1000)}"
            )
            return FolderIndexStats(
                total_files=0,
                indexed_files=0,
                nodes_count=0,
                chunks_count=0,
                indexed_chunks_count=0,
            )

        # 2) Process each file
        namespace = self.build_folder_namespace(project_id=project_id, folder_path=folder_path)

        total_nodes = 0
        total_chunks = 0
        total_indexed = 0
        indexed_files = 0

        for i, file_node in enumerate(indexable_files):
            try:
                t_file = time.perf_counter()
                stats = await self._index_file_node(
                    file_node=file_node,
                    namespace=namespace,
                    s3_service=s3_service,
                    project_id=project_id,
                    user_id=user_id,
                )
                total_nodes += stats.nodes_count
                total_chunks += stats.chunks_count
                total_indexed += stats.indexed_chunks_count
                indexed_files += 1

                log_info(
                    f"[index_folder] file_indexed: {i + 1}/{total_files} "
                    f"file_path={file_node.path} name={file_node.name} "
                    f"chunks={stats.chunks_count} elapsed_ms={int((time.perf_counter() - t_file) * 1000)}"
                )

                # Call progress callback if provided
                if progress_callback:
                    try:
                        progress_callback(indexed_files, total_files)
                    except Exception as e:
                        log_error(f"[index_folder] progress_callback error: {e}")

            except Exception as e:
                log_error(
                    f"[index_folder] file_error: file_path={file_node.path} "
                    f"name={file_node.name} error={e}"
                )
                # Continue with other files even if one fails
                continue

        log_info(
            f"[index_folder] done: folder_path={folder_path} "
            f"files={indexed_files}/{total_files} nodes={total_nodes} "
            f"chunks={total_chunks} total_ms={int((time.perf_counter() - t0) * 1000)}"
        )

        return FolderIndexStats(
            total_files=total_files,
            indexed_files=indexed_files,
            nodes_count=total_nodes,
            chunks_count=total_chunks,
            indexed_chunks_count=total_indexed,
        )

    async def _index_file_node(
        self,
        *,
        file_node: VersionEntry,
        namespace: str,
        s3_service: S3Service,
        project_id: str,
        user_id: str,
    ) -> SearchIndexStats:
        """
        Index a single file entry (json or markdown) into the folder namespace.
        """
        t0 = time.perf_counter()

        is_markdown = file_node.type == "markdown"
        is_json = file_node.type == "json"

        try:
            content_bytes = self._ops.for_user(project_id, user_id).read_file(
                project_id, file_node.path
            )
        except Exception:
            content_bytes = None

        if is_json:
            if content_bytes:
                import json as _json_mod

                try:
                    content_data = _json_mod.loads(content_bytes.decode("utf-8"))
                except Exception:
                    content_data = {}
            else:
                content_data = {}
            scope_pointer = ""
        elif is_markdown:
            if content_bytes:
                content_data = content_bytes.decode("utf-8", errors="replace")
                scope_pointer = "/"
            else:
                log_info(f"[_index_file_node] skip: no content for markdown {file_node.path}")
                return SearchIndexStats(nodes_count=0, chunks_count=0, indexed_chunks_count=0)
        else:
            log_info(f"[_index_file_node] skip: unsupported type={file_node.type}")
            return SearchIndexStats(nodes_count=0, chunks_count=0, indexed_chunks_count=0)

        # 2) Extract large string nodes or use content directly
        t2 = time.perf_counter()
        if is_json:
            # For JSON, extract large string nodes
            nodes = await asyncio.to_thread(
                lambda: list(
                    iter_large_string_nodes_for_chunking(
                        self._chunking_service,
                        content_data,
                        self._chunking_config,
                        base_pointer=scope_pointer,
                    )
                )
            )
        else:
            # For markdown, create a single "node" with the whole content
            from src.infra.chunking.schemas import LargeStringNode

            if len(content_data) >= self._chunking_config.chunk_threshold_chars:
                nodes = [LargeStringNode(json_pointer="/", content=content_data)]
            else:
                nodes = []

        log_info(
            f"[_index_file_node] extract_nodes: file={file_node.id} nodes={len(nodes)} "
            f"elapsed_ms={int((time.perf_counter() - t2) * 1000)}"
        )

        if not nodes:
            return SearchIndexStats(nodes_count=0, chunks_count=0, indexed_chunks_count=0)

        # 3) Ensure chunks (idempotent)
        t3 = time.perf_counter()
        all_chunks: list[Chunk] = []
        file_id = file_node.path
        for n in nodes:
            ensured = await asyncio.to_thread(
                ensure_chunks_for_pointer,
                repo=self._chunk_repo,
                service=self._chunking_service,
                path=file_id,
                json_pointer=n.json_pointer,
                content=n.content,
                config=self._chunking_config,
            )
            all_chunks.extend(list(ensured.chunks))

        log_info(
            f"[_index_file_node] ensure_chunks: file={file_node.path} chunks={len(all_chunks)} "
            f"elapsed_ms={int((time.perf_counter() - t3) * 1000)}"
        )

        if not all_chunks:
            return SearchIndexStats(nodes_count=len(nodes), chunks_count=0, indexed_chunks_count=0)

        # 4) Generate embeddings
        t4 = time.perf_counter()
        texts = [c.chunk_text for c in all_chunks]
        vectors = await self._embedding.generate_embeddings_batch(texts)
        log_info(
            f"[_index_file_node] embedding: file={file_node.path} vectors={len(vectors)} "
            f"elapsed_ms={int((time.perf_counter() - t4) * 1000)}"
        )

        # 5) Turbopuffer upsert with file metadata
        t5 = time.perf_counter()
        upsert_rows: list[dict[str, Any]] = []
        doc_ids: list[str] = []

        for c, vec in zip(all_chunks, vectors, strict=True):
            doc_id = self.build_folder_doc_id(
                file_path=file_id,
                json_pointer=c.json_pointer,
                content_hash=c.content_hash,
                chunk_index=c.chunk_index,
            )
            doc_ids.append(doc_id)
            upsert_rows.append(
                {
                    "id": doc_id,
                    "vector": vec,
                    "json_pointer": c.json_pointer,
                    "chunk_index": c.chunk_index,
                    "total_chunks": c.total_chunks,
                    "char_start": c.char_start,
                    "char_end": c.char_end,
                    "content_hash": c.content_hash,
                    "chunk_id": int(c.id),
                    "file_path": file_id,
                    "file_version_path": file_node.path,
                    "file_name": file_node.name,
                    "file_type": file_node.type,
                }
            )

        async with self._write_lease_factory(project_id, "search.index_folder"):
            await self._tp.write(
                namespace,
                upsert_rows=upsert_rows,
                distance_metric="cosine_distance",
            )
        log_info(
            f"[_index_file_node] turbopuffer: file={file_node.path} rows={len(upsert_rows)} "
            f"elapsed_ms={int((time.perf_counter() - t5) * 1000)}"
        )

        # 6) Update chunks table with turbopuffer info (best-effort)
        for c, doc_id in zip(all_chunks, doc_ids, strict=True):
            try:  # noqa: SIM105 -- Keep the best-effort indexing boundary explicit.
                await asyncio.to_thread(
                    lambda cid=int(c.id), did=doc_id: (
                        self._chunk_repo._client.table("chunks")
                        .update(
                            {
                                "turbopuffer_namespace": namespace,
                                "turbopuffer_doc_id": did,
                            }
                        )
                        .eq("id", cid)
                        .execute()
                    )
                )
            except Exception:
                pass  # Don't block on chunk update failures

        log_info(
            f"[_index_file_node] done: file={file_node.path} "
            f"total_ms={int((time.perf_counter() - t0) * 1000)}"
        )

        return SearchIndexStats(
            nodes_count=len(nodes),
            chunks_count=len(all_chunks),
            indexed_chunks_count=len(all_chunks),
        )

    async def search_folder(
        self,
        *,
        project_id: str,
        folder_path: str,
        query: str,
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        """
        Search in a folder namespace, returning results with file path information.
        """
        q = (query or "").strip()
        if not q:
            raise ValueError("query must be non-empty")

        top_k = int(top_k)
        if top_k <= 0:
            raise ValueError("top_k must be > 0")
        if top_k > 20:
            top_k = 20

        namespace = self.build_folder_namespace(project_id=project_id, folder_path=folder_path)

        # 1) Generate query embedding and search
        query_vec = await self._embedding.generate_embedding(q)
        resp = await self._tp.query(
            namespace,
            rank_by=("vector", "ANN", query_vec),
            top_k=top_k,
            include_attributes=True,
        )
        rows = resp.rows or []

        # 2) Batch fetch chunk_text from DB
        chunk_ids: list[int] = []
        for r in rows[:top_k]:
            attrs = r.attributes or {}
            cid = attrs.get("chunk_id")
            try:
                if cid is not None:
                    chunk_ids.append(int(cid))
            except (TypeError, ValueError):
                pass

        chunks = await asyncio.to_thread(self._chunk_repo.get_by_ids, chunk_ids)
        chunk_text_by_id = {int(c.id): c.chunk_text for c in chunks}

        # 3) Build output with file information
        out: list[dict[str, Any]] = []
        for r in rows[:top_k]:
            attrs = r.attributes or {}

            # Extract file information
            file_path_val = str(attrs.get("file_path") or "")
            file_version_path = str(
                attrs.get("file_version_path") or attrs.get("file_id_path") or ""
            )
            file_name = str(attrs.get("file_name") or "")
            file_type = str(attrs.get("file_type") or "")

            # Extract chunk information
            json_pointer = str(attrs.get("json_pointer") or "")
            json_pointer = _normalize_json_pointer(json_pointer)

            cid = attrs.get("chunk_id")
            chunk_id_int: int | None = None
            try:
                if cid is not None:
                    chunk_id_int = int(cid)
            except (TypeError, ValueError):
                chunk_id_int = None

            # Calculate score
            score = 0.0
            if r.score is not None:
                score = float(r.score)
            elif r.distance is not None:
                try:
                    dist = float(r.distance)
                    score = 1.0 / (1.0 + dist)
                except (TypeError, ValueError):
                    score = 0.0

            out.append(
                {
                    "score": float(score),
                    "file": {
                        "path": file_path_val,
                        "version_path": file_version_path,
                        "name": file_name,
                        "type": file_type,
                    },
                    "chunk": {
                        "id": chunk_id_int,
                        "json_pointer": json_pointer,
                        "chunk_index": int(attrs.get("chunk_index") or 0),
                        "total_chunks": int(attrs.get("total_chunks") or 0),
                        "chunk_text": (
                            chunk_text_by_id.get(chunk_id_int, "")
                            if chunk_id_int is not None
                            else ""
                        ),
                    },
                }
            )

        return out
