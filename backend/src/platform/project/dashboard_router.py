"""Non-resource Dashboard sections; resource_dashboard alone owns HTTP routing.

Resource identities and usage are domain-qualified in resource_dashboard, never
joined through a mixed Connection DTO or unqualified ID set.
"""

from pydantic import BaseModel

from src.version_engine.adapters.product.operation_adapter import ProductOperationAdapter


class DashboardProject(BaseModel):
    id: str
    name: str
    description: str | None = None


class DashboardNodeCounts(BaseModel):
    total: int = 0
    folders: int = 0
    files: int = 0


class DashboardTool(BaseModel):
    id: str
    name: str
    type: str | None = None
    index_status: str | None = None
    chunks_count: int | None = None
    total_files: int | None = None
    indexed_files: int | None = None


class DashboardUpload(BaseModel):
    id: str
    status: str
    type: str
    progress: int = 0
    message: str | None = None


_NODE_COUNT_CACHE: dict[tuple[str, str], DashboardNodeCounts] = {}
_NODE_COUNT_CACHE_MAX = 1024


def _compute_node_counts(ops: ProductOperationAdapter, project_id: str) -> DashboardNodeCounts:
    """Cache complete Git tree counts by Project and head, never by path/name."""
    try:
        head_commit = ops.get_head_commit_id(project_id)
    except Exception:
        # A failed head read may use a live tree read, not an empty success.
        head_commit = ""

    cache_key = (project_id, head_commit)
    if head_commit and cache_key in _NODE_COUNT_CACHE:
        return _NODE_COUNT_CACHE[cache_key]

    all_entries = ops.list_tree(project_id, "", max_depth=-1)
    folder_count = sum(1 for entry in all_entries if entry.type == "folder")
    file_count = sum(1 for entry in all_entries if entry.type != "folder")
    counts = DashboardNodeCounts(
        total=folder_count + file_count, folders=folder_count, files=file_count,
    )
    if head_commit:
        if len(_NODE_COUNT_CACHE) >= _NODE_COUNT_CACHE_MAX:
            _NODE_COUNT_CACHE.clear()
        _NODE_COUNT_CACHE[cache_key] = counts
    return counts


def _fetch_tools(sb, project_id: str) -> list[DashboardTool]:
    tool_rows = (
        sb.table("tools")
        .select("id, name, type")
        .eq("project_id", project_id)
        .execute()
    ).data
    index_map = _build_index_map(sb, tool_rows)
    tools: list[DashboardTool] = []
    for tool in tool_rows:
        index = index_map.get(tool["id"])
        tools.append(DashboardTool(
            id=tool["id"],
            name=tool["name"],
            type=tool.get("type"),
            index_status=index["status"] if index else None,
            chunks_count=index.get("chunks_count") if index else None,
            total_files=index.get("total_files") if index else None,
            indexed_files=index.get("indexed_files") if index else None,
        ))
    return tools


def _build_index_map(sb, tool_rows: list) -> dict[str, dict]:
    search_tool_ids = [tool["id"] for tool in tool_rows if tool.get("type") == "search"]
    if not search_tool_ids:
        return {}
    rows = (
        sb.table("search_index_tasks")
        .select("id, status, result")
        .in_("id", search_tool_ids)
        .execute()
    ).data
    status_map = {"running": "indexing", "completed": "ready", "failed": "error", "pending": "pending"}
    result = {}
    for row in rows:
        metadata = row.get("result") or {}
        result[row["id"]] = {
            "status": status_map.get(row.get("status", ""), row.get("status", "")),
            "chunks_count": metadata.get("chunks_count"),
            "total_files": metadata.get("total_files"),
            "indexed_files": metadata.get("indexed_files"),
        }
    return result


def _fetch_uploads(sb, project_id: str) -> list[DashboardUpload]:
    # Dependency/schema failures must fail the inventory, never look like no jobs.
    upload_rows = (
        sb.table("uploads")
        .select("id, status, type, progress, message")
        .neq("type", "search_index")
        .eq("project_id", project_id)
        .in_("status", ["pending", "running"])
        .limit(20)
        .execute()
    ).data
    search_rows = (
        sb.table("search_index_tasks")
        .select("id, status, progress, message")
        .eq("project_id", project_id)
        .in_("status", ["pending", "running"])
        .limit(20)
        .execute()
    ).data or []
    rows = (upload_rows or []) + [{**row, "type": "search_index"} for row in search_rows]
    return [
        DashboardUpload(
            id=row["id"],
            status=row["status"],
            type=row.get("type", "file"),
            progress=row.get("progress", 0),
            message=row.get("message"),
        )
        for row in rows[:20]
    ]
