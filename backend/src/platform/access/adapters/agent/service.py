"""
Agent Service — Orchestrator

The frontend only needs to pass active_tool_ids; the backend handles everything automatically:
1. Look up tool configuration from the database by tool_id
2. If it is a bash tool, automatically fetch table data and start a sandbox
3. If it is a search tool, register it with Claude and call SearchService directly
4. Build the Claude request
5. Sandbox write-back via CollaborationService (L2)

Supported node types:
- folder: recursively fetch child files and rebuild directory structure in the sandbox
- json: export content as data.json
- file/pdf/image/etc: download a single file

Supported tool types:
- bash (sandbox): configured via agent_bash, executed in a data sandbox
- search: linked via agent_tool, vector retrieval (Turbopuffer)
"""

import asyncio
import contextlib
import json
import time
import time as time_module  # For latency tracking
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any

from loguru import logger

from src.config import settings
from src.platform.access.adapters.agent.chat.service import ChatService
from src.platform.access.adapters.agent.config.service import AgentConfigService
from src.platform.access.adapters.agent.request_builder import (
    _default_anthropic_client,
    _get_bash_tool,
    _sanitize_tool_name,
)
from src.platform.access.adapters.agent.sandbox_session import SandboxData, SandboxFile, prepare_sandbox_data
from src.platform.access.adapters.agent.schemas import AgentRequest
from src.platform.analytics.service import log_context_access
from src.platform.repository_target.models import RepositoryPathProjection
from src.version_engine.adapters.product.operation_adapter import ProductOperationAdapter


@dataclass
class SearchToolConfig:
    """Search Tool configuration associated with an Agent."""

    tool_id: str
    path: str
    project_id: str
    node_type: str
    name: str  # Original tool name
    description: str  # Tool description
    claude_tool_name: str  # Tool name registered with Claude


class AgentService:
    """Agent core logic."""

    def __init__(self, anthropic_client=None):
        self._anthropic = anthropic_client or _default_anthropic_client()

    async def execute_task_sync(
        self,
        agent_id: str,
        task_content: str,
        user_id: str,
        ops: ProductOperationAdapter | None,
        sandbox_service,
        s3_service=None,
        agent_config_service=None,
        max_iterations: int = 15,
    ) -> dict:
        """
        Non-streaming Agent task execution (used by Schedule Agent).

        Reuses the core logic of stream_events but without streaming output or chat history.

        Args:
            agent_id: Agent ID
            task_content: Task content (from agent.task_content)
            user_id: User ID (agent owner)
            ops: ProductOperationAdapter for reading version tree (writes go through InProcessVersionClient)
            sandbox_service: SandboxService
            s3_service: S3Service (optional)
            agent_config_service: AgentConfigService
            max_iterations: Maximum number of iterations

        Returns:
            dict with execution results
        """
        from src.config import settings

        result = {
            "status": "success",
            "output_summary": "",
            "tool_calls": [],
            "updated_nodes": [],
        }

        try:
            # ========== 1. Get Agent configuration ==========
            if not agent_config_service:
                return {"status": "failed", "error": "agent_config_service is required"}

            agent = agent_config_service.get_agent(agent_id)
            if not agent:
                return {"status": "failed", "error": f"Agent not found: {agent_id}"}

            # Verify access via project
            if not agent_config_service.is_visible_to(agent_id, user_id):
                return {"status": "failed", "error": "Unauthorized access to agent"}

            logger.info(f"[ScheduleAgent] Executing agent: {agent.name} (id={agent_id})")

            # ========== 2. Collect bash tools ==========
            bash_tools: list[dict] = []
            for ba in agent.bash_accesses:
                bash_tools.append(
                    {
                        "path": ba.path,
                        "readonly": ba.readonly,
                    }
                )
                logger.info(f"[ScheduleAgent] Found bash access: path={ba.path}")

            use_bash = len(bash_tools) > 0
            sandbox_readonly = all(tool["readonly"] for tool in bash_tools) if bash_tools else True

            # ========== 3. Prepare sandbox data ==========
            sandbox_data: SandboxData | None = None
            sandbox_session_id = None
            node_path_map: dict = {}

            if use_bash and ops:
                all_files: list[SandboxFile] = []
                primary_node_type = "folder"
                primary_path = ""
                primary_node_name = ""

                for i, tool in enumerate(bash_tools):
                    try:
                        data = await prepare_sandbox_data(
                            ops=ops,
                            project_id=agent.project_id,
                            path=tool["path"],
                        )
                        logger.info(
                            f"[ScheduleAgent] Prepared sandbox data: path={tool['path']}, files={len(data.files)}"
                        )
                        all_files.extend(data.files)

                        if data.files:
                            main_path = data.files[0].path
                            node_path_map[tool["path"]] = {
                                "path": main_path,
                                "node_type": data.node_type,
                                "readonly": tool["readonly"],
                            }

                        if i == 0:
                            primary_node_type = data.node_type
                            primary_path = data.root_path
                            primary_node_name = data.root_node_name
                    except Exception as e:
                        logger.warning(f"[ScheduleAgent] Failed to prepare sandbox data: {e}")

                sandbox_data = SandboxData(
                    files=all_files,
                    node_type=primary_node_type if len(bash_tools) == 1 else "multi",
                    root_path=primary_path,
                    root_node_name=primary_node_name,
                    node_path_map=node_path_map,
                )

            # ========== 4. Start sandbox ==========
            if use_bash and sandbox_service:
                sandbox_session_id = f"schedule-{uuid.uuid4()}"

                if sandbox_data and sandbox_data.node_type == "json" and len(bash_tools) == 1:
                    json_content = {}
                    if sandbox_data.files:
                        try:
                            json_content = json.loads(sandbox_data.files[0].content or "{}")
                        except (TypeError, ValueError):
                            json_content = {}
                    start_result = await sandbox_service.start(
                        session_id=sandbox_session_id,
                        data=json_content,
                        readonly=sandbox_readonly,
                        audit_context={
                            "source": "schedule_agent",
                            "user_id": user_id,
                            "agent_id": agent_id,
                            "project_id": agent.project_id,
                        },
                    )
                else:
                    start_result = await sandbox_service.start_with_files(
                        session_id=sandbox_session_id,
                        files=sandbox_data.files if sandbox_data else [],
                        readonly=sandbox_readonly,
                        s3_service=s3_service,
                        audit_context={
                            "source": "schedule_agent",
                            "user_id": user_id,
                            "agent_id": agent_id,
                            "project_id": agent.project_id,
                        },
                    )

                if not start_result.get("success"):
                    return {
                        "status": "failed",
                        "error": start_result.get("error", "Failed to start sandbox"),
                    }

                logger.info(f"[ScheduleAgent] Sandbox started: {sandbox_session_id}")

            # ========== 5. Build Claude request ==========
            tools: list[dict[str, Any]] = [_get_bash_tool()] if use_bash else []

            if use_bash and sandbox_data:
                node_type = sandbox_data.node_type
                if node_type == "json":
                    system_prompt = "You are an AI agent specialized in JSON data processing. Use the bash tool to view and manipulate data in /workspace/data.json."
                elif node_type == "folder":
                    system_prompt = "You are an AI agent with access to a file system. Use the bash tool to explore and manipulate files in /workspace/."
                elif node_type == "multi":
                    system_prompt = "You are an AI agent with access to multiple files and folders. Use the bash tool to explore and manipulate files in /workspace/."
                else:
                    system_prompt = f"You are an AI agent with access to a {node_type} file. Use the bash tool to analyze the file in /workspace/."
            else:
                system_prompt = "You are an AI agent, a helpful assistant."

            # Build user message
            user_content = task_content
            if use_bash and sandbox_data:
                mode_str = "⚠️ Read-only mode" if sandbox_readonly else "✏️ Read-write mode"
                context_prefix = (
                    f"[Data Context]\n"
                    f"Working directory: /workspace/\n"
                    f"Number of files: {len(sandbox_data.files)}\n"
                    f"Permissions: {mode_str}\n"
                    f"[Task]\n"
                )
                user_content = context_prefix + task_content

            messages = [{"role": "user", "content": user_content}]

            # ========== 6. Call Claude (non-streaming loop) ==========
            iterations = 0
            all_text_outputs = []

            while iterations < max_iterations:
                iterations += 1
                logger.info(f"[ScheduleAgent] Claude iteration {iterations}")

                try:
                    # Build API call parameters (omit tools when empty to avoid proxy gateway compatibility issues)
                    create_kwargs: dict[str, Any] = {
                        "model": settings.ANTHROPIC_MODEL,
                        "max_tokens": 4096,
                        "system": system_prompt,
                        "messages": messages,
                    }
                    if tools:
                        create_kwargs["tools"] = tools

                    response = await self._anthropic.messages.create(**create_kwargs)

                    stop_reason = response.stop_reason
                    content_blocks = response.content

                    # Process response content
                    tool_uses = []
                    response_content = []

                    for block in content_blocks:
                        block_type = getattr(block, "type", None)
                        if block_type == "text":
                            text = getattr(block, "text", "")
                            all_text_outputs.append(text)
                            response_content.append({"type": "text", "text": text})
                        elif block_type == "tool_use":
                            tool_uses.append(
                                {
                                    "id": getattr(block, "id", ""),
                                    "name": getattr(block, "name", ""),
                                    "input": getattr(block, "input", {}),
                                }
                            )
                            response_content.append(
                                {
                                    "type": "tool_use",
                                    "id": getattr(block, "id", ""),
                                    "name": getattr(block, "name", ""),
                                    "input": getattr(block, "input", {}),
                                }
                            )

                    logger.info(
                        f"[ScheduleAgent] Claude response: stop_reason={stop_reason}, tool_uses={len(tool_uses)}"
                    )

                    # No tool calls, finish
                    if not tool_uses:
                        break

                    # Execute tools
                    tool_results = []
                    for tool in tool_uses:
                        tool_name = tool.get("name", "")
                        tool_input = tool.get("input", {})

                        if tool_name == "bash" and use_bash and sandbox_service:
                            command = tool_input.get("command", "")
                            logger.info(f"[ScheduleAgent] Executing bash: {command[:100]}")

                            exec_result = await sandbox_service.exec(
                                sandbox_session_id,
                                command,
                                audit_context={
                                    "source": "schedule_agent",
                                    "agent_id": agent_id,
                                    "session_id": sandbox_session_id,
                                    "project_id": agent.project_id,
                                },
                            )

                            if exec_result.get("success"):
                                output = exec_result.get("output", "")
                                result["tool_calls"].append(
                                    {
                                        "command": command,
                                        "output": output[:500],
                                        "success": True,
                                    }
                                )
                            else:
                                output = exec_result.get("error", "")
                                result["tool_calls"].append(
                                    {
                                        "command": command,
                                        "output": output[:500],
                                        "success": False,
                                    }
                                )
                        else:
                            output = f"Unknown tool: {tool_name}"

                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": tool.get("id", ""),
                                "content": output,
                            }
                        )

                    messages.append({"role": "assistant", "content": response_content})
                    messages.append({"role": "user", "content": tool_results})

                    if stop_reason == "end_turn":
                        break

                except Exception as e:
                    logger.error(f"[ScheduleAgent] Claude error: {e}")
                    result["status"] = "failed"
                    result["error"] = str(e)
                    break

            # ========== 7. Write data back through Version Engine ==========
            if use_bash and sandbox_service and sandbox_session_id and not sandbox_readonly:
                if sandbox_data and sandbox_data.node_path_map:
                    agent_identity = f"agent:{agent.id}" if agent else "agent:unknown"

                    from src.platform.access.adapters.agent.sandbox_session import _read_modified_files
                    from src.version_engine.adapters.batch.in_process_client import (
                        InProcessVersionClient,
                    )
                    from src.version_engine.bootstrap.dependencies import (
                        build_worker_version_engine_container,
                    )

                    # _read_modified_files returns (modified, deleted); the
                    # chat path unpacks it but the schedule path used to assign
                    # the whole 2-tuple to ``modified_files`` — so ``modified``
                    # was a (dict, list) tuple (not a dict), deletions were
                    # never applied, and the count/loop below were wrong.
                    modified_files, deleted_files = await _read_modified_files(
                        sandbox_service,
                        sandbox_session_id,
                        {},
                        "/workspace",
                        "",
                    )
                    if modified_files or deleted_files:
                        try:
                            repo_manager = build_worker_version_engine_container().repo_manager
                            scope_path = sandbox_data.root_path or ""
                            client = InProcessVersionClient(
                                repo_manager,
                                agent.project_id,
                                RepositoryPathProjection(path_prefix=scope_path),
                                actor=agent_identity,
                            )
                            await asyncio.to_thread(client.clone)
                            from src.version_engine.derived.hooks import push_and_finalize

                            push_result = await push_and_finalize(
                                client,
                                agent.project_id,
                                repo_manager=repo_manager,
                                modified=modified_files,
                                deleted=deleted_files,
                                message="Schedule Agent write-back",
                                who=agent_identity,
                            )
                            logger.info(
                                f"[ScheduleAgent] version push: commit={push_result.get('commit_id') or '(none)'} "
                                f"files={len(modified_files)} deleted={len(deleted_files)}"
                            )
                            for path in modified_files:
                                result["updated_nodes"].append(
                                    {
                                        "nodeId": path,
                                        "nodeName": path.rsplit("/", 1)[-1]
                                        if "/" in path
                                        else path,
                                    }
                                )
                        except Exception as e:
                            logger.warning(f"[ScheduleAgent] version push failed: {e}")

                # Stop sandbox
                await sandbox_service.stop(sandbox_session_id)
                logger.info("[ScheduleAgent] Sandbox stopped")
            elif sandbox_session_id and sandbox_service:
                await sandbox_service.stop(sandbox_session_id)

            # ========== 8. Return results ==========
            result["output_summary"] = "\n".join(all_text_outputs)[:2000]
            logger.info(f"[ScheduleAgent] Execution completed: {result['status']}")
            return result

        except Exception as e:
            logger.error(f"[ScheduleAgent] Execution failed: {e}")
            return {"status": "failed", "error": str(e)}

    async def stream_events(
        self,
        request: AgentRequest,
        current_user,
        ops: ProductOperationAdapter | None,
        tool_service,
        sandbox_service,
        chat_service: ChatService | None = None,
        s3_service=None,
        agent_config_service: AgentConfigService | None = None,
        search_service=None,
        max_iterations: int = 15,
    ) -> AsyncGenerator[dict, None]:
        """
        Main entry point for handling Agent requests.

        Supported tool types:
        1. bash (sandbox) — configured via agent_bash
        2. search — search-type tools linked via agent_tool
        """

        # ========== 1. Parse configuration, prefer new agent_access ==========
        bash_tools: list[dict] = []  # [{path, readonly}, ...]

        logger.info(
            f"[Agent DEBUG] agent_id={request.agent_id}, active_tool_ids={request.active_tool_ids}, user_id={current_user.user_id if current_user else None}"
        )

        # New version: if agent_id exists, read configuration from agent_bash table
        if request.agent_id and current_user and agent_config_service:
            try:
                agent = agent_config_service.get_agent(request.agent_id)
                logger.info(
                    f"[Agent DEBUG] Got agent: {agent.id if agent else None}, project_id={agent.project_id if agent else None}"
                )

                # Verify access: check whether the user has permission to access this Agent via project_id
                # Note: the Agent model has no user_id field; verification goes through the project table
                has_access = False
                if agent:
                    has_access = agent_config_service.is_visible_to(
                        request.agent_id, current_user.user_id
                    )
                    logger.info(f"[Agent DEBUG] Access check: has_access={has_access}")

                if agent and has_access:
                    logger.info(
                        f"[Agent] Found agent config: id={agent.id}, bash_accesses={len(agent.bash_accesses)}"
                    )
                    # Collect all Bash access permissions (in the new architecture all bash_accesses are terminal accesses)
                    for bash in agent.bash_accesses:
                        bash_tools.append(
                            {
                                "path": bash.path,
                                "readonly": bash.readonly,
                            }
                        )
                        logger.info(f"[Agent] Found bash access from agent_bash: path={bash.path}")
                    logger.info(f"[Agent] Total bash accesses collected: {len(bash_tools)}")
                else:
                    logger.warning(
                        f"[Agent] Agent not found or unauthorized: agent_id={request.agent_id}, has_access={has_access}"
                    )
                    if not agent:
                        raise ValueError(f"Agent not found: {request.agent_id}")
                    if not has_access:
                        raise PermissionError(f"Not authorized to use agent: {request.agent_id}")
            except (ValueError, PermissionError):
                raise  # Let these propagate to the SSE error handler
            except Exception as e:
                logger.warning(f"[Agent] Failed to get agent config: {e}", exc_info=True)

        # Shell/bash access is managed exclusively via agent_bash.
        # See architecture: agents -> agent_bash (data access) + agent_tool (tool bindings)

        # ========== 1b. Collect Search Tools (from agent_tool bindings) ==========
        search_tools_map: dict[str, SearchToolConfig] = {}  # {claude_tool_name: SearchToolConfig}

        if (
            request.agent_id
            and current_user
            and agent_config_service
            and tool_service
            and search_service
        ):
            try:
                agent_for_tools = agent_config_service.get_agent(request.agent_id)

                if agent_for_tools and agent_for_tools.tools:
                    used_names: set[str] = set()
                    for agent_tool_binding in agent_for_tools.tools:
                        if not agent_tool_binding.enabled:
                            continue

                        # Load full info from tool table
                        tool_info = tool_service.get_by_id(agent_tool_binding.tool_id)
                        if not tool_info or tool_info.type != "search":
                            continue

                        # Get node info to determine search type
                        try:
                            node = (
                                ops.stat(agent_for_tools.project_id, tool_info.path)
                                if ops
                                else None
                            )
                            if not node:
                                continue
                        except Exception:
                            continue

                        # Generate a Claude-compatible tool name (avoid conflicts)
                        base_name = _sanitize_tool_name(tool_info.name)
                        claude_name = f"search_{base_name}"
                        if claude_name in used_names:
                            claude_name = f"search_{base_name}_{tool_info.id[:8]}"
                        used_names.add(claude_name)

                        search_tools_map[claude_name] = SearchToolConfig(
                            tool_id=tool_info.id,
                            path=tool_info.path,
                            project_id=tool_info.project_id or agent_for_tools.project_id,
                            node_type=node.type or "json",
                            name=tool_info.name,
                            description=tool_info.description or f"Search in {tool_info.name}",
                            claude_tool_name=claude_name,
                        )
                        logger.info(
                            f"[Agent] Loaded search tool: {claude_name} (tool_id={tool_info.id}, node_type={node.type})"
                        )

                    if search_tools_map:
                        logger.info(f"[Agent] Total search tools: {len(search_tools_map)}")
            except Exception as e:
                logger.warning(f"[Agent] Failed to load search tools: {e}", exc_info=True)

        # ========== 2. Chat persistence (best-effort) ==========
        persisted_session_id: str | None = None
        created_session = False
        should_persist = current_user is not None and chat_service is not None

        logger.info(
            f"[Chat Persist] should_persist={should_persist}, current_user={current_user is not None}, chat_service={chat_service is not None}"
        )

        if should_persist:
            try:
                persisted_session_id, created_session = chat_service.ensure_session(
                    user_id=current_user.user_id,
                    session_id=request.session_id,
                    agent_id=request.agent_id,
                    mode="agent",
                )
                logger.info(
                    f"[Chat Persist] Session ready: id={persisted_session_id}, created={created_session}"
                )
                # If this is a newly created session, set the title first
                if created_session and persisted_session_id:
                    try:
                        chat_service.maybe_set_title_on_first_message(
                            user_id=current_user.user_id,
                            session_id=persisted_session_id,
                            first_message=request.prompt,
                        )
                        logger.info(f"[Chat Persist] Session title set for {persisted_session_id}")
                    except Exception as e:
                        logger.warning(f"[Chat Persist] Failed to set session title: {e}")
                # Always emit session event so client can track session_id across calls
                if persisted_session_id:
                    yield {"type": "session", "sessionId": persisted_session_id}
            except Exception as e:
                logger.error(f"[Chat Persist] Failed to ensure session: {e}")
                persisted_session_id = None
                should_persist = False

        # Load history messages
        messages: list[dict[str, Any]] = []
        if should_persist and persisted_session_id:
            try:
                history = chat_service.load_history_for_llm(
                    user_id=current_user.user_id, session_id=persisted_session_id, limit=60
                )
                messages.extend(history)
            except Exception:
                if request.chatHistory:
                    for item in request.chatHistory:
                        messages.append({"role": item.role, "content": item.content})
        else:
            if request.chatHistory:
                for item in request.chatHistory:
                    messages.append({"role": item.role, "content": item.content})

        # Save user message
        if should_persist and persisted_session_id:
            try:
                chat_service.add_user_message(
                    session_id=persisted_session_id, content=request.prompt
                )
                logger.info(f"[Chat Persist] User message saved to session {persisted_session_id}")
            except Exception as e:
                logger.error(f"[Chat Persist] Failed to save user message: {e}")

        # ========== 3. If bash tools exist, prepare sandbox data and start sandbox ==========
        use_bash = len(bash_tools) > 0
        sandbox_data: SandboxData | None = None
        sandbox_session_id = None
        live_sandbox_session = None
        _agent_project_id = ""
        # If any access is not readonly, the entire sandbox is not readonly
        sandbox_readonly = all(tool["readonly"] for tool in bash_tools) if bash_tools else True

        if use_bash and ops and current_user:
            # Collect files from all bash accesses
            all_files: list[SandboxFile] = []
            primary_node_type = "folder"  # default type
            primary_path = ""
            primary_node_name = ""
            # Track each node's sandbox path and type for write-back
            node_path_map: dict = {}  # {path: {path, node_type, readonly}}

            # Determine project_id from agent config
            _agent_project_id = ""
            if request.agent_id and agent_config_service:
                _agent_obj = agent_config_service.get_agent(request.agent_id)
                if _agent_obj:
                    _agent_project_id = _agent_obj.project_id

            for i, tool in enumerate(bash_tools):
                try:
                    data = await prepare_sandbox_data(
                        ops=ops,
                        project_id=_agent_project_id,
                        path=tool["path"],
                    )
                    logger.info(
                        f"[Agent] Prepared sandbox data for access {i + 1}/{len(bash_tools)}: "
                        f"path={tool['path']}, type={data.node_type}, files={len(data.files)}"
                    )
                    all_files.extend(data.files)

                    # Log context access (data egress tracking)
                    log_context_access(
                        path=tool["path"],
                        node_type=data.node_type,
                        node_name=data.root_node_name,
                        user_id=current_user.user_id if current_user else None,
                        agent_id=request.agent_id,
                        session_id=request.session_id,
                        project_id=_agent_project_id,
                    )

                    # Record path mapping (for write-back and display)
                    if data.node_type == "folder" and data.files:
                        # Folder type: create an independent write-back mapping for each child file
                        for f in data.files:
                            if f.path and f.node_type in ("json", "markdown"):
                                node_path_map[f.path] = {
                                    "path": f.path,
                                    "node_type": f.node_type,
                                    "readonly": tool["readonly"],
                                    "base_commit_id": f.base_commit_id,
                                    "base_content": f.content,  # record original content at the time Agent reads it
                                }
                        # Also record the folder itself (for display)
                        node_path_map[tool["path"]] = {
                            "path": f"/workspace/{data.root_node_name}"
                            if data.root_node_name
                            else "/workspace",
                            "node_type": "folder",
                            "readonly": tool["readonly"],
                            "is_folder_parent": True,
                        }
                    elif data.files:
                        # Non-folder type (JSON, single file, etc.): record main file path
                        main_file = data.files[0]
                        node_path_map[tool["path"]] = {
                            "path": main_file.path,
                            "node_type": data.node_type,
                            "readonly": tool["readonly"],
                            "base_commit_id": main_file.base_commit_id,
                            "base_content": main_file.content,
                        }
                    else:
                        # Record empty folders too, using the folder name as the path
                        node_path_map[tool["path"]] = {
                            "path": f"/workspace/{data.root_node_name}"
                            if data.root_node_name
                            else "/workspace/(empty folder)",
                            "node_type": data.node_type,
                            "readonly": tool["readonly"],
                            "is_empty": True,
                        }

                    # The first access determines the primary type
                    if i == 0:
                        primary_node_type = data.node_type
                        primary_path = data.root_path
                        primary_node_name = data.root_node_name
                except Exception as e:
                    logger.warning(
                        f"[Agent] Failed to prepare sandbox data for node {tool['path']}: {e}"
                    )

            sandbox_data = SandboxData(
                files=all_files,
                node_type=primary_node_type
                if len(bash_tools) == 1
                else "multi",  # mark as multi when there are multiple
                root_path=primary_path,
                root_node_name=primary_node_name,
                node_path_map=node_path_map,
            )
            logger.info(
                f"[Agent] Total sandbox files: {len(all_files)} from {len(bash_tools)} accesses, path_map={list(node_path_map.keys())}"
            )

        if use_bash and sandbox_service:
            from src.platform.access.adapters.agent.sandbox_session import AgentSandboxSession

            sandbox_parent_path = ""

            # A chat turn owns exactly one execution and never publishes its
            # non-serializable Version client into a process-global registry.
            # Provider lifecycle is tracked by the execution subsystem; this
            # local object exists only to perform the request's final write-back.
            sandbox_session_id = f"agent-{uuid.uuid4()}"

            if sandbox_data and sandbox_data.node_type == "json" and len(bash_tools) == 1:
                json_content = {}
                if sandbox_data.files:
                    try:
                        json_content = json.loads(sandbox_data.files[0].content or "{}")
                    except (TypeError, ValueError):
                        json_content = {}
                start_result = await sandbox_service.start(
                    session_id=sandbox_session_id,
                    data=json_content,
                    readonly=sandbox_readonly,
                    audit_context={
                        "source": "chat_agent",
                        "user_id": current_user.user_id if current_user else None,
                        "agent_id": request.agent_id,
                        "project_id": _agent_project_id,
                    },
                )
            else:
                start_result = await sandbox_service.start_with_files(
                    session_id=sandbox_session_id,
                    files=sandbox_data.files if sandbox_data else [],
                    readonly=sandbox_readonly,
                    s3_service=s3_service,
                    audit_context={
                        "source": "chat_agent",
                        "user_id": current_user.user_id if current_user else None,
                        "agent_id": request.agent_id,
                        "project_id": _agent_project_id,
                    },
                )

            if start_result.get("success"):
                scope_path = ""
                if sandbox_data and _agent_project_id and ops:
                    root_path = sandbox_data.root_path
                    root_entry = ops.stat(_agent_project_id, root_path) if root_path else None
                    if root_entry:
                        if root_entry.type == "folder":
                            sandbox_parent_path = root_path
                            scope_path = root_path.strip("/")
                        else:
                            sandbox_parent_path = (
                                root_path.rsplit("/", 1)[0] if "/" in root_path else ""
                            )
                            scope_path = sandbox_parent_path.strip("/")

                version_client = None
                cloned_files = {}
                repo_manager = None
                if _agent_project_id and not sandbox_readonly:
                    from src.version_engine.adapters.batch.in_process_client import (
                        InProcessVersionClient,
                    )
                    from src.version_engine.bootstrap.dependencies import (
                        build_worker_version_engine_container,
                    )

                    repo_manager = build_worker_version_engine_container().repo_manager
                    version_client = InProcessVersionClient(
                        repo_manager,
                        _agent_project_id,
                        RepositoryPathProjection(path_prefix=scope_path),
                        actor=f"agent:{request.agent_id}",
                    )
                    cloned_files = await asyncio.to_thread(version_client.clone)

                now = time.time()
                live_sandbox_session = AgentSandboxSession(
                    chat_session_id=persisted_session_id or sandbox_session_id,
                    sandbox_session_id=sandbox_session_id,
                    agent_id=request.agent_id,
                    version_client=version_client,
                    cloned_files=cloned_files,
                    scope_path=scope_path,
                    created_at=now,
                    last_active=now,
                    readonly=sandbox_readonly,
                    project_id=_agent_project_id,
                    parent_path=sandbox_parent_path,
                    repo_manager=repo_manager,
                )

            if not start_result.get("success"):
                err_msg = start_result.get("error", "Failed to start sandbox")
                yield {"type": "error", "message": err_msg}
                if should_persist and persisted_session_id:
                    with contextlib.suppress(Exception):
                        chat_service.add_assistant_message(
                            session_id=persisted_session_id,
                            content=err_msg,
                            parts=[{"type": "text", "content": err_msg}],
                        )
                return

            yield {"type": "status", "message": "Sandbox ready"}

        # ========== 4. Build Claude request ==========
        tools: list[dict[str, Any]] = [_get_bash_tool()] if use_bash else []

        # Register Search Tools with Claude
        for claude_name, stc in search_tools_map.items():
            tools.append(
                {
                    "name": claude_name,
                    "description": stc.description,
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "The search query text",
                            },
                            "top_k": {
                                "type": "integer",
                                "description": "Number of results to return (default 5, max 20)",
                                "default": 5,
                            },
                        },
                        "required": ["query"],
                    },
                }
            )

        use_search = len(search_tools_map) > 0

        # Build system prompt based on node type
        if use_bash and sandbox_data:
            node_type = sandbox_data.node_type
            if node_type == "json":
                system_prompt = "You are an AI agent specialized in JSON data processing. Use the bash tool to view and manipulate data in /workspace/data.json."
            elif node_type == "folder":
                system_prompt = "You are an AI agent with access to a file system. Use the bash tool to explore and manipulate files in /workspace/."
            elif node_type == "multi":
                system_prompt = "You are an AI agent with access to multiple files and folders. Use the bash tool to explore and manipulate files in /workspace/."
            else:
                system_prompt = f"You are an AI agent with access to a {node_type} file. Use the bash tool to analyze the file in /workspace/."
        else:
            system_prompt = "You are an AI agent, a helpful assistant."

        # If search tools exist, add supplementary description to the system prompt
        if use_search:
            search_tool_descriptions = []
            for claude_name, stc in search_tools_map.items():
                search_tool_descriptions.append(
                    f"  - {claude_name}: {stc.description} (data source: {stc.name})"
                )
            search_prompt_suffix = (
                "\n\n[Available Search Tools]\n"
                "You have access to the following search tools for semantic retrieval:\n"
                + "\n".join(search_tool_descriptions)
                + "\nUse these tools when the user asks questions about the data. "
                "Pass a natural language query to search for relevant information."
            )
            system_prompt += search_prompt_suffix

        # Build user message: if using bash, add data context
        user_content = request.prompt
        if use_bash and sandbox_data:
            node_type = sandbox_data.node_type
            node_path_map = sandbox_data.node_path_map or {}

            # Generate detailed permissions list
            def build_access_list() -> str:
                lines = []
                for tool in bash_tools:
                    path_info = node_path_map.get(tool["path"], {})
                    path = path_info.get("path", "/workspace/(unknown)")
                    mode = "👁️ View Only" if tool["readonly"] else "✏️ Editable"
                    is_empty = path_info.get("is_empty", False)
                    suffix = " 📁 (empty folder)" if is_empty else ""
                    lines.append(f"  - {path} ({mode}){suffix}")
                return "\n".join(lines) if lines else "  - /workspace/ (unknown)"

            if node_type == "json" and len(bash_tools) == 1:
                mode_str = (
                    "⚠️ Read-only mode - changes will not be saved"
                    if sandbox_readonly
                    else "✏️ Read-write mode"
                )
                context_prefix = (
                    f"[Data Context]\n"
                    f"Node type: JSON\n"
                    f"Data file: /workspace/data.json\n"
                    f"Permissions: {mode_str}\n"
                    f"[User Message]\n"
                )
            elif node_type == "folder" and len(bash_tools) == 1:
                mode_str = (
                    "⚠️ Read-only mode - changes will not be saved"
                    if sandbox_readonly
                    else "✏️ Read-write mode"
                )
                context_prefix = (
                    f"[Data Context]\n"
                    f"Node type: Folder\n"
                    f"Folder name: {sandbox_data.root_node_name}\n"
                    f"Working directory: /workspace/\n"
                    f"Number of files: {len(sandbox_data.files)}\n"
                    f"Permissions: {mode_str}\n"
                    f"[User Message]\n"
                )
            elif node_type == "multi" or len(bash_tools) > 1:
                # Multiple accesses - show detailed permissions list
                access_list = build_access_list()
                context_prefix = (
                    f"[Data Context]\n"
                    f"Working directory: /workspace/\n"
                    f"Total files: {len(sandbox_data.files)}\n\n"
                    f"📂 Accessible resources:\n"
                    f"{access_list}\n\n"
                    f"⚠️ Important notes:\n"
                    f"  - Only content at the above paths will be saved\n"
                    f"  - New files cannot be created in the /workspace/ root directory\n"
                    f"  - Changes to View Only paths will not be persisted\n"
                    f"[User Message]\n"
                )
            else:
                # Single file such as file/pdf/image etc.
                file_name = (
                    sandbox_data.files[0].path.split("/")[-1] if sandbox_data.files else "file"
                )
                mode_str = (
                    "⚠️ Read-only mode - changes will not be saved"
                    if sandbox_readonly
                    else "✏️ Read-write mode"
                )
                context_prefix = (
                    f"[Data Context]\n"
                    f"Node type: {node_type}\n"
                    f"File name: {file_name}\n"
                    f"File path: /workspace/{file_name}\n"
                    f"Permissions: {mode_str}\n"
                    f"[User Message]\n"
                )
            user_content = context_prefix + request.prompt

        messages.append({"role": "user", "content": user_content})

        # ========== 5. Call Claude (streaming), handle tool calls ==========
        persisted_parts: list[dict[str, Any]] = []
        tool_index = 0
        iterations = 0

        while iterations < max_iterations:
            iterations += 1

            logger.info(f"[CLAUDE REQUEST] Iteration {iterations} (streaming)")
            logger.info(f"[CLAUDE DEBUG] system_prompt = {system_prompt}")
            logger.info(f"[CLAUDE DEBUG] tools = {json.dumps(tools, ensure_ascii=False)}")
            logger.info(
                f"[CLAUDE DEBUG] messages = {json.dumps(messages, ensure_ascii=False, default=str)[:2000]}"
            )

            try:
                # ===== Streaming call to Claude =====
                current_text_content = ""
                tool_uses: list[dict[str, Any]] = []
                current_tool: dict[str, Any] | None = None
                current_tool_input_json = ""
                stop_reason = None
                response_content: list[Any] = []

                # Build API call parameters (omit tools when empty to avoid proxy gateway compatibility issues)
                stream_kwargs: dict[str, Any] = {
                    "model": settings.ANTHROPIC_MODEL,
                    "max_tokens": 4096,
                    "system": system_prompt,
                    "messages": messages,
                }
                if tools:
                    stream_kwargs["tools"] = tools

                async with self._anthropic.messages.stream(**stream_kwargs) as stream:
                    async for event in stream:
                        event_type = getattr(event, "type", None)

                        if event_type == "content_block_start":
                            block = getattr(event, "content_block", None)
                            block_type = getattr(block, "type", None) if block else None

                            if block_type == "text":
                                current_text_content = ""
                            elif block_type == "tool_use":
                                current_tool = {
                                    "id": getattr(block, "id", ""),
                                    "name": getattr(block, "name", ""),
                                    "input": {},
                                }
                                current_tool_input_json = ""

                        elif event_type == "content_block_delta":
                            delta = getattr(event, "delta", None)
                            delta_type = getattr(delta, "type", None) if delta else None

                            if delta_type == "text_delta":
                                text = getattr(delta, "text", "")
                                if text:
                                    current_text_content += text
                                    # Yield each text fragment in real time!
                                    yield {"type": "text_delta", "content": text}

                            elif delta_type == "input_json_delta":
                                partial_json = getattr(delta, "partial_json", "")
                                if partial_json:
                                    current_tool_input_json += partial_json

                        elif event_type == "content_block_stop":
                            if current_text_content:
                                # Text block finished, save complete text
                                persisted_parts.append(
                                    {"type": "text", "content": current_text_content}
                                )
                                response_content.append(
                                    {"type": "text", "text": current_text_content}
                                )
                                current_text_content = ""

                            if current_tool:
                                # Tool block finished, parse JSON input
                                try:
                                    current_tool["input"] = (
                                        json.loads(current_tool_input_json)
                                        if current_tool_input_json
                                        else {}
                                    )
                                except json.JSONDecodeError:
                                    current_tool["input"] = {"raw": current_tool_input_json}

                                tool_uses.append(current_tool)
                                response_content.append(
                                    {
                                        "type": "tool_use",
                                        "id": current_tool["id"],
                                        "name": current_tool["name"],
                                        "input": current_tool["input"],
                                    }
                                )
                                current_tool = None
                                current_tool_input_json = ""

                        elif event_type == "message_delta":
                            delta = getattr(event, "delta", None)
                            if delta:
                                stop_reason = getattr(delta, "stop_reason", None)

                logger.info(
                    f"[CLAUDE RESPONSE] Iteration {iterations}: stop_reason={stop_reason}, tool_uses={len(tool_uses)}"
                )

            except Exception as e:
                msg = str(e)
                logger.error(f"[CLAUDE ERROR] {msg}")
                yield {"type": "error", "message": msg}
                if should_persist and persisted_session_id:
                    with contextlib.suppress(Exception):
                        chat_service.add_assistant_message(
                            session_id=persisted_session_id,
                            content=msg,
                            parts=[{"type": "text", "content": msg}],
                        )
                if sandbox_session_id and sandbox_service:
                    with contextlib.suppress(Exception):
                        await sandbox_service.stop(sandbox_session_id)
                return

            # No tool calls, exit loop
            if not tool_uses:
                break

            # Execute tools
            tool_results = []
            for tool in tool_uses:
                current_tool_index = tool_index
                tool_index += 1
                tool_name = tool.get("name", "")
                tool_input = tool.get("input", {})

                yield {
                    "type": "tool_start",
                    "toolId": current_tool_index,
                    "toolName": tool_name,
                    "toolInput": tool_input.get("command")
                    if tool_name == "bash"
                    else json.dumps(tool_input),
                }

                persisted_parts.append(
                    {
                        "type": "tool",
                        "toolId": str(current_tool_index),
                        "toolName": tool_name or "tool",
                        "toolInput": tool_input.get("command")
                        if tool_name == "bash"
                        else json.dumps(tool_input),
                        "toolStatus": "running",
                    }
                )

                success = True
                output = ""

                if tool_name == "bash" and use_bash and sandbox_service:
                    command = tool_input.get("command", "")

                    exec_result = await sandbox_service.exec(
                        sandbox_session_id,
                        command,
                        audit_context={
                            "source": "chat_agent",
                            "user_id": current_user.user_id if current_user else None,
                            "agent_id": request.agent_id,
                            "session_id": persisted_session_id,
                            "project_id": _agent_project_id,
                        },
                    )

                    if exec_result.get("success"):
                        output = exec_result.get("output", "")
                    else:
                        success = False
                        output = exec_result.get("error", "")
                elif tool_name in search_tools_map and search_service:
                    # ===== Search Tool execution =====
                    stc = search_tools_map[tool_name]
                    query = tool_input.get("query", "")
                    top_k = tool_input.get("top_k", 5)

                    exec_start = time_module.time()
                    try:
                        if stc.node_type == "folder":
                            results = await search_service.search_folder(
                                project_id=stc.project_id,
                                folder_path=stc.path,
                                query=query,
                                top_k=top_k,
                            )
                        else:
                            results = await search_service.search_scope(
                                project_id=stc.project_id,
                                path=stc.path,
                                tool_json_path="",
                                query=query,
                                top_k=top_k,
                            )
                        exec_latency = int((time_module.time() - exec_start) * 1000)
                        output = json.dumps(results, ensure_ascii=False, indent=2)
                        success = True
                        logger.info(
                            f"[Agent] Search tool executed: {tool_name}, query='{query}', "
                            f"results={len(results)}, latency={exec_latency}ms"
                        )
                    except Exception as e:
                        exec_latency = int((time_module.time() - exec_start) * 1000)
                        output = f"Search error: {e!s}"
                        success = False
                        logger.error(
                            f"[Agent] Search tool failed: {tool_name}, error={e}, latency={exec_latency}ms"
                        )
                else:
                    output = f"Unknown tool: {tool_name}"
                    success = False

                yield {
                    "type": "tool_end",
                    "toolId": current_tool_index,
                    "toolName": tool_name,
                    "output": output[:500],
                    "success": success,
                }

                # Update persisted state
                for i in range(len(persisted_parts) - 1, -1, -1):
                    p = persisted_parts[i]
                    if p.get("type") == "tool" and p.get("toolId") == str(current_tool_index):
                        p["toolStatus"] = "completed" if success else "error"
                        p["toolOutput"] = output[:500]
                        break

                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool.get("id", ""),
                        "content": output,
                        "is_error": not success,
                    }
                )

            messages.append({"role": "assistant", "content": response_content})
            messages.append({"role": "user", "content": tool_results})

            if stop_reason == "end_turn":
                break

        # ========== 6. Save results, clean up sandbox ==========
        logger.info(
            f"[Chat Persist] Attempting to save assistant message: should_persist={should_persist}, session_id={persisted_session_id}, parts_count={len(persisted_parts)}"
        )
        if should_persist and persisted_session_id:
            try:
                for p in persisted_parts:
                    if p.get("type") == "tool" and p.get("toolStatus") == "running":
                        p["toolStatus"] = "completed"
                final_content = "\n\n".join(
                    [p.get("content", "") for p in persisted_parts if p.get("type") == "text"]
                ).strip()
                logger.info(
                    f"[Chat Persist] Saving assistant message: content_length={len(final_content)}, parts={persisted_parts}"
                )
                chat_service.add_assistant_message(
                    session_id=persisted_session_id, content=final_content, parts=persisted_parts
                )
                logger.info("[Chat Persist] Assistant message saved successfully!")
            except Exception as e:
                logger.error(f"[Chat Persist] Failed to save assistant message: {e}")
        else:
            logger.warning(
                f"[Chat Persist] Skipping assistant message save: should_persist={should_persist}, session_id={persisted_session_id}"
            )

        if use_bash and sandbox_service and sandbox_session_id:
            from src.platform.access.adapters.agent.sandbox_session import (
                _read_modified_files,
            )

            updated_nodes = []
            if (
                live_sandbox_session
                and live_sandbox_session.version_client
                and not live_sandbox_session.readonly
            ):
                try:
                    modified, deleted = await _read_modified_files(
                        sandbox_service,
                        live_sandbox_session.sandbox_session_id,
                        live_sandbox_session.cloned_files,
                        "/workspace",
                        live_sandbox_session.scope_path,
                    )
                    if modified or deleted:
                        from src.version_engine.derived.hooks import push_and_finalize

                        push_result = await push_and_finalize(
                            live_sandbox_session.version_client,
                            live_sandbox_session.project_id,
                            repo_manager=live_sandbox_session.repo_manager,
                            modified=modified,
                            deleted=deleted,
                            message=f"Agent chat write-back ({len(modified)} modified, {len(deleted)} deleted)",
                            who=f"agent:{request.agent_id}",
                        )
                        live_sandbox_session.cloned_files.update(modified)
                        for dp in deleted:
                            live_sandbox_session.cloned_files.pop(dp, None)
                        logger.info(
                            f"[Agent] version push: commit={push_result.get('commit_id') or '(none)'} "
                            f"merged={push_result.get('merged', False)} modified={len(modified)} deleted={len(deleted)}"
                        )
                        for path in modified:
                            node_name = path.rsplit("/", 1)[-1] if "/" in path else path
                            updated_nodes.append(
                                {
                                    "nodeId": path,
                                    "nodeName": node_name,
                                    "mergeStrategy": "version_push",
                                }
                            )
                except Exception as e:
                    logger.error(f"[Agent] Write-back failed: {e}", exc_info=True)

            if updated_nodes:
                yield {
                    "type": "result",
                    "success": True,
                    "updatedNodes": updated_nodes,
                }
            else:
                yield {"type": "result", "success": True}

            try:
                await sandbox_service.stop(sandbox_session_id)
            except Exception as exc:
                logger.error(f"[Agent] Sandbox cleanup failed: {exc}")

        else:
            yield {"type": "result", "success": True}
