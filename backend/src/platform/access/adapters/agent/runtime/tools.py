"""Reuse configured search tools; no worker-selected Project or credential."""

import asyncio
import json
import re
from types import SimpleNamespace

from src.infra.search.dependencies import get_search_service
from src.infra.search.schemas import SearchToolQueryInput
from src.platform.access.adapters.agent.runtime.publication import allowed
from src.tool.dependencies import get_tool_service


class BoundTools:
    def __init__(self, admission):
        self.admission = admission
        self.service = get_tool_service(admission.authorization)
        self.search = get_search_service(admission.authorization)

    def definitions(self, run):
        tools = []
        for definition in run["policy"]["tool_definitions"]:
            tool = SimpleNamespace(**definition)
            if tool is None or tool.project_id != run["project_id"]:
                raise PermissionError("Agent tool belongs to another Project")
            if tool.type != "search" or (
                tool.path is None or not allowed(tool.path, run["policy"])
            ):
                raise PermissionError("Agent tool exceeds the execution policy")
            tools.append(
                {
                    "name": "search_" + re.sub("[^a-zA-Z0-9_]", "_", tool.id),
                    "description": tool.description or tool.name,
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "top_k": {"type": "integer", "minimum": 1, "maximum": 20},
                        },
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                }
            )
        return tools

    async def execute(self, run, name, values):
        candidate = next(
            (
                tid
                for tid in run["policy"]["tool_ids"]
                if name == "search_" + re.sub("[^a-zA-Z0-9_]", "_", tid)
            ),
            None,
        )
        if candidate is None:
            raise PermissionError("Tool is not bound to this Agent")
        definition = next(
            (value for value in run["policy"]["tool_definitions"] if value["id"] == candidate), None
        )
        tool = SimpleNamespace(**definition) if definition else None
        if (
            tool is None
            or tool.type != "search"
            or tool.project_id != run["project_id"]
            or (tool.path is None or not allowed(tool.path, run["policy"]))
        ):
            raise PermissionError("Tool scope changed")
        value = SearchToolQueryInput.model_validate(values)
        # Scope service performs the canonical indexed lookup; Project identity
        # and search boundary always come from the persisted tool configuration.
        entry = await asyncio.to_thread(self.search._ops.stat, run["project_id"], tool.path)
        if entry.type == "folder":
            result = await self.search.search_folder(
                project_id=run["project_id"],
                folder_path=tool.path,
                query=value.query,
                top_k=value.top_k,
            )
            result = [
                item
                for item in result
                if item.get("file", {}).get("version_path")
                and allowed(item["file"]["version_path"], run["policy"])
            ]
        else:
            result = await self.search.search_scope(
                project_id=run["project_id"],
                path=tool.path,
                tool_json_path=tool.json_path,
                query=value.query,
                top_k=value.top_k,
            )
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(result, ensure_ascii=False, default=str)[:100000],
                }
            ]
        }
