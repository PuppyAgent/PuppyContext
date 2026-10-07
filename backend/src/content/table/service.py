"""
Table (Knowledge Base) Content Management

All Tables must belong to a project.
Write operations use Write Engine (ProductOperationAdapter),
read operations read JSON content from version ObjectStore.
The tables table in DB is only used for storing metadata indexes.
"""

import json
from datetime import UTC
from typing import Any

import jmespath
from jsonpointer import resolve_pointer

from src.content.table.models import Table
from src.content.table.repository import TableRepositoryBase
from src.content.table.schemas import ProjectWithTables
from src.exceptions import BusinessException, ErrorCode, NotFoundException
from src.utils.logger import log_info


class TableService:
    def __init__(
        self,
        repo: TableRepositoryBase,
        version_write=None,
        repo_manager=None,
        node_repo=None,
    ):
        self.repo = repo
        self._version_writer = version_write
        self._repos = repo_manager
        self._node_repo = node_repo
        self._user_id = None
        self._read_states = {}

    def bind_user(self, user_id):
        self._user_id = user_id

    # ================================================================
    # ObjectStore helpers
    # ================================================================

    def _ensure_version_repo(self):
        if self._repos is None:
            raise BusinessException(
                "version repo_manager not configured",
                code=ErrorCode.INTERNAL_SERVER_ERROR,
            )

    def _get_ops(self, project_id):
        if not self._user_id:
            raise PermissionError("table content requires an explicit principal")
        from src.version_engine.adapters.product.operation_adapter import ProductOperationAdapter

        return ProductOperationAdapter(self._repos).for_user(project_id, self._user_id)

    def _get_write_commands(self, project_id):
        from src.version_engine.adapters.product.commands import VersionWriteCommandService

        return VersionWriteCommandService(self._get_ops(project_id))

    def _table_version_path(self, _project_id, table_id):
        return f"tables/{table_id}.json"

    def _read_json_from_version_store(self, project_id, version_path):
        from src.version_engine.domain.intents import ProjectWriteState

        ops = self._get_ops(project_id)
        with ops.open_read(project_id, ops._grant) as reader:
            data = json.loads(reader.read_file(project_id, version_path))
            self._read_states[project_id] = ProjectWriteState(
                project_id,
                "",
                repository_grant=ops._grant,
                repository_revision=reader.get_read_revision(project_id),
            )
            return data

    def _read_table_data(self, table):
        self._ensure_version_repo()
        if not table.project_id:
            raise ValueError("Table has no Project")
        return self._read_json_from_version_store(
            table.project_id, self._table_version_path(table.project_id, table.id)
        )

    # ================================================================
    # Read-only queries (from DB index or ObjectStore)
    # ================================================================

    def get_projects_with_tables_by_org_id(self, org_id: str) -> list[ProjectWithTables]:
        return self.repo.get_projects_with_tables_by_org_id(org_id)

    def get_by_id(self, table_id: str) -> Table | None:
        return self.repo.get_by_id(table_id)

    # ================================================================
    # Write operations - all through Write Engine (ProductOperationAdapter)
    # ================================================================

    async def create(
        self,
        user_id: str,
        name: str,
        description: str,
        data: dict,
        project_id: str,
    ) -> Table:
        self.bind_user(user_id)
        self._ensure_version_repo()

        from src.utils.id_generator import generate_uuid_v7

        table_id = generate_uuid_v7()
        version_path = self._table_version_path(project_id, table_id)

        table_blob = {
            "id": table_id,
            "name": name,
            "description": description,
            "data": data,
        }
        content = json.dumps(table_blob, ensure_ascii=False, indent=2).encode("utf-8")

        commands = self._get_write_commands(project_id)
        await commands.write_bytes(
            project_id,
            version_path,
            content,
            actor=f"user:{user_id}",
            message=f"create table {name}",
        )
        log_info(f"[Table] Created table {table_id} via Version Engine")

        # Persist to Supabase index table
        try:
            table = self.repo.create(
                created_by=user_id,
                name=name,
                description=description,
                data=data,
                project_id=project_id,
                table_id=table_id,
            )
            return table
        except Exception as e:
            log_info(f"[Table] Failed to persist table {table_id} to index: {e}")
            # version write succeeded, return synthetic object
            from datetime import datetime

            return Table(
                id=table_id,
                name=name,
                project_id=project_id,
                created_by=user_id,
                description=description,
                data=data,
                created_at=datetime.now(UTC),
            )

    async def update(
        self,
        table_id: str,
        name: str | None,
        description: str | None,
        data: dict | None,
    ) -> Table:
        self._ensure_version_repo()

        table = self.repo.get_by_id(table_id)
        if not table:
            raise NotFoundException(f"Table not found: {table_id}", code=ErrorCode.NOT_FOUND)

        if not table.project_id:
            raise BusinessException(
                "Table has no project_id, cannot update via Version Engine",
                code=ErrorCode.BAD_REQUEST,
            )

        version_path = self._table_version_path(table.project_id, table_id)
        current_data = self._read_table_data(table)

        table_blob = {
            "id": table_id,
            "name": name or table.name,
            "description": description if description is not None else table.description,
            "data": data if data is not None else current_data.get("data", table.data),
        }
        content = json.dumps(table_blob, ensure_ascii=False, indent=2).encode("utf-8")

        commands = self._get_write_commands(table.project_id)
        await commands.write_bytes(
            table.project_id,
            version_path,
            content,
            project_write_state=self._read_states.get(table.project_id),
            actor="system:table_update",
            message=f"update table {table_id}",
        )
        log_info(f"[Table] Updated table {table_id} via Version Engine")

        updated = self.repo.get_by_id(table_id)
        return updated or table

    async def delete(self, table_id: str) -> None:
        table = self.repo.get_by_id(table_id)
        if not table:
            raise NotFoundException(f"Table not found: {table_id}", code=ErrorCode.NOT_FOUND)

        if not table.project_id:
            raise BusinessException(
                "Table has no project_id, cannot delete via Version Engine",
                code=ErrorCode.BAD_REQUEST,
            )

        self._ensure_version_repo()
        version_path = self._table_version_path(table.project_id, table_id)
        commands = self._get_write_commands(table.project_id)
        await commands.delete(
            table.project_id,
            [version_path],
            actor="system:table_delete",
            message=f"delete table {table_id}",
        )
        log_info(f"[Table] Deleted table {table_id} via Version Engine")

    # ================================================================
    # Context Data operations — JSON Pointer + version write
    # ================================================================

    @staticmethod
    def _resolve_parent(
        actual_data: Any, pointer_path: str, *, not_found_exc: type = BusinessException
    ) -> Any:
        """Resolve a JSON pointer path and return the parent node, raising on failure."""
        try:
            parent = resolve_pointer(actual_data, pointer_path, None)
        except Exception as e:
            raise BusinessException(f"Invalid path: {e!s}", code=ErrorCode.BAD_REQUEST)
        if parent is None:
            raise not_found_exc(
                f"Path not found: {pointer_path}",
                code=ErrorCode.BAD_REQUEST
                if not_found_exc is BusinessException
                else ErrorCode.NOT_FOUND,
            )
        return parent

    @staticmethod
    def _require_element_field(element: dict, field: str) -> None:
        if field not in element:
            raise BusinessException(
                f"Element missing '{field}' field", code=ErrorCode.VALIDATION_ERROR
            )

    def _insert_into_dict(self, parent: dict, elements: list[dict]) -> None:
        """Validate and insert elements into a dict parent node."""
        for element in elements:
            self._require_element_field(element, "key")
            if element["key"] in parent:
                raise BusinessException(
                    f"Key '{element['key']}' already exists", code=ErrorCode.VALIDATION_ERROR
                )

        existing_content_strs = {json.dumps(parent[k], sort_keys=True) for k in parent}
        for element in elements:
            self._require_element_field(element, "content")
            content_str = json.dumps(element["content"], sort_keys=True)
            if content_str in existing_content_strs:
                raise BusinessException(
                    f"Content already exists for key: {element['key']}",
                    code=ErrorCode.VALIDATION_ERROR,
                )

        for element in elements:
            parent[element["key"]] = element["content"]

    def _insert_into_list(self, parent: list, elements: list[dict]) -> None:
        """Validate and append elements into a list parent node."""
        for element in elements:
            self._require_element_field(element, "content")
            parent.append(element["content"])

    def _update_dict_elements(self, parent: dict, elements: list[dict]) -> None:
        """Validate and update elements in a dict parent node."""
        for element in elements:
            self._require_element_field(element, "key")
            if element["key"] not in parent:
                raise NotFoundException(
                    f"Key '{element['key']}' not found", code=ErrorCode.NOT_FOUND
                )
        for element in elements:
            self._require_element_field(element, "content")
            parent[element["key"]] = element["content"]

    def _update_list_elements(self, parent: list, elements: list[dict]) -> None:
        """Validate and update elements in a list parent node."""
        for element in elements:
            self._require_element_field(element, "key")
            self._require_element_field(element, "content")
            try:
                idx = int(element["key"])
            except (TypeError, ValueError):
                raise BusinessException(
                    f"Invalid list index: {element['key']}",
                    code=ErrorCode.BAD_REQUEST,
                )
            if idx < 0 or idx >= len(parent):
                raise NotFoundException(f"Index '{idx}' not found", code=ErrorCode.NOT_FOUND)
            parent[idx] = element["content"]

    @staticmethod
    def _delete_dict_keys(parent: dict, keys: list[str]) -> None:
        """Validate and delete keys from a dict parent node."""
        for key in keys:
            if key not in parent:
                raise NotFoundException(f"Key '{key}' not found", code=ErrorCode.NOT_FOUND)
        for key in keys:
            del parent[key]

    @staticmethod
    def _delete_list_indices(parent: list, keys: list[str]) -> None:
        """Validate and delete indices from a list parent node."""
        indices: list[int] = []
        for key in keys:
            try:
                idx = int(key)
            except (TypeError, ValueError):
                raise BusinessException(f"Invalid list index: {key}", code=ErrorCode.BAD_REQUEST)
            if idx < 0 or idx >= len(parent):
                raise NotFoundException(f"Index '{idx}' not found", code=ErrorCode.NOT_FOUND)
            indices.append(idx)
        for idx in sorted(set(indices), reverse=True):
            del parent[idx]

    def _get_table_and_data(self, table_id: str) -> tuple:
        """Fetch table, read its data, and return (table, data_copy, actual_data)."""
        table = self.repo.get_by_id(table_id)
        if not table:
            raise NotFoundException(f"Table not found: {table_id}", code=ErrorCode.NOT_FOUND)
        data = self._read_table_data(table).copy()
        actual_data = data.get("data", data) if "data" in data else data
        return table, data, actual_data

    async def create_context_data(
        self, table_id: str, mounted_json_pointer_path: str, elements: list[dict]
    ) -> Any:
        table, data, actual_data = self._get_table_and_data(table_id)
        parent = self._resolve_parent(actual_data, mounted_json_pointer_path)

        if isinstance(parent, dict):
            self._insert_into_dict(parent, elements)
        elif isinstance(parent, list):
            self._insert_into_list(parent, elements)
        else:
            raise BusinessException("Path points to non-dict/list node", code=ErrorCode.BAD_REQUEST)

        await self._write_table_data(table, data)
        return resolve_pointer(actual_data, mounted_json_pointer_path)

    def get_context_data(self, table_id: str, json_pointer_path: str) -> Any:
        table = self.repo.get_by_id(table_id)
        if not table:
            raise NotFoundException(f"Table not found: {table_id}", code=ErrorCode.NOT_FOUND)

        data = self._read_table_data(table)
        actual_data = data.get("data", data) if "data" in data else data

        try:
            result = resolve_pointer(actual_data, json_pointer_path, None)
            if result is None:
                raise NotFoundException(
                    f"Path not found: {json_pointer_path}", code=ErrorCode.NOT_FOUND
                )
            return result
        except Exception as e:
            if isinstance(e, NotFoundException):
                raise
            raise BusinessException(f"Invalid path: {e!s}", code=ErrorCode.BAD_REQUEST)

    async def update_context_data(
        self, table_id: str, json_pointer_path: str, elements: list[dict]
    ) -> Any:
        table, data, actual_data = self._get_table_and_data(table_id)
        parent = self._resolve_parent(
            actual_data, json_pointer_path, not_found_exc=NotFoundException
        )

        if isinstance(parent, dict):
            self._update_dict_elements(parent, elements)
        elif isinstance(parent, list):
            self._update_list_elements(parent, elements)
        else:
            raise BusinessException("Path points to non-dict/list node", code=ErrorCode.BAD_REQUEST)

        await self._write_table_data(table, data)
        return resolve_pointer(actual_data, json_pointer_path)

    async def delete_context_data(
        self, table_id: str, json_pointer_path: str, keys: list[str]
    ) -> Any:
        table, data, actual_data = self._get_table_and_data(table_id)
        parent = self._resolve_parent(
            actual_data, json_pointer_path, not_found_exc=NotFoundException
        )

        if isinstance(parent, dict):
            self._delete_dict_keys(parent, keys)
        elif isinstance(parent, list):
            self._delete_list_indices(parent, keys)
        else:
            raise BusinessException("Path points to non-dict/list node", code=ErrorCode.BAD_REQUEST)

        await self._write_table_data(table, data)
        return resolve_pointer(actual_data, json_pointer_path)

    async def _write_table_data(self, table: Table, full_blob: dict) -> None:
        """Write the complete table JSON to ObjectStore (single write point)"""
        self._ensure_version_repo()
        if not table.project_id:
            raise BusinessException(
                "Table has no project_id, cannot write to ObjectStore",
                code=ErrorCode.BAD_REQUEST,
            )
        version_path = self._table_version_path(table.project_id, table.id)
        content = json.dumps(full_blob, ensure_ascii=False, indent=2).encode("utf-8")
        commands = self._get_write_commands(table.project_id)
        await commands.write_bytes(
            table.project_id,
            version_path,
            content,
            project_write_state=self._read_states.get(table.project_id),
            actor="system:table_edit",
            message=f"edit table data {table.id}",
        )

    # ================================================================
    # Query operations (read-only)
    # ================================================================

    def query_context_data_with_jmespath(
        self, table_id: str, json_pointer_path: str, query: str
    ) -> Any | None:
        table = self.repo.get_by_id(table_id)
        if not table:
            raise NotFoundException(f"Table not found: {table_id}", code=ErrorCode.NOT_FOUND)

        try:
            data = self._read_table_data(table)
            actual_data = data.get("data", data) if "data" in data else data
            base_data = resolve_pointer(actual_data, json_pointer_path, None)
            if base_data is None:
                raise NotFoundException(
                    f"Path not found: {json_pointer_path}", code=ErrorCode.NOT_FOUND
                )

            result = jmespath.search(query, base_data)
            return result

        except jmespath.exceptions.ParseError as e:
            raise BusinessException(f"JMESPath syntax error: {e!s}", code=ErrorCode.BAD_REQUEST)
        except Exception as e:
            if isinstance(e, NotFoundException):
                raise
            raise BusinessException(f"Query failed: {e!s}", code=ErrorCode.BAD_REQUEST)

    def get_context_structure(self, table_id: str, json_pointer_path: str) -> dict:
        table = self.repo.get_by_id(table_id)
        if not table:
            raise NotFoundException(f"Table not found: {table_id}", code=ErrorCode.NOT_FOUND)

        try:
            data = self._read_table_data(table)
            actual_data = data.get("data", data) if "data" in data else data
            target = resolve_pointer(actual_data, json_pointer_path, None)
            if target is None:
                raise NotFoundException(
                    f"Path not found: {json_pointer_path}", code=ErrorCode.NOT_FOUND
                )

            return self._extract_structure(target)

        except Exception as e:
            if isinstance(e, NotFoundException):
                raise
            raise BusinessException(
                f"Failed to extract structure: {e!s}",
                code=ErrorCode.INTERNAL_SERVER_ERROR,
            )

    def _extract_structure(self, data: Any) -> Any:
        if isinstance(data, dict):
            return {key: self._extract_structure(value) for key, value in data.items()}
        elif isinstance(data, list):
            if len(data) > 0:
                return [self._extract_structure(data[0])]
            return []
        else:
            return f"<{type(data).__name__}>"
