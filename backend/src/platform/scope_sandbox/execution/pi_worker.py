"""Pi-specific process transport using the existing sandbox provider registry.

No host mounts, model keys or shell interpolation of project data. Providers
expose the same framed channel. This module does not own Agent run state.
"""

import asyncio
import codecs
import json
import time
from contextlib import suppress
from uuid import uuid4

from src.config import settings
from src.infra.supabase.instrumentation import database_stage
from src.platform.scope_sandbox.execution.store import ExecutionSession, durable_execution_store
from src.platform.scope_sandbox.execution.worker_port import WorkerDisconnected, WorkerLost

MAX_FRAME = 192 * 1024 * 1024
SEND_TIMEOUT = 15


class PiWorker:
    def __init__(self, execution_id, project_id, *, store=None, provider=None, image=None):
        self.execution_id, self.project_id = execution_id, project_id
        self.store = store if store is not None else durable_execution_store()
        self.provider = provider or (
            "e2b"
            if settings.SANDBOX_TYPE == "e2b"
            or (settings.SANDBOX_TYPE == "auto" and settings.E2B_API_KEY)
            else "docker"
        )
        self.image = image or settings.CLOUD_AGENT_IMAGE
        self.artifact = settings.CLOUD_AGENT_E2B_TEMPLATE if self.provider == "e2b" else self.image
        self.reused = False
        self.parked = False
        self.timeout_renewal = time.monotonic()
        self.queue = asyncio.Queue(maxsize=32)
        self.buffer = ""
        self.process = None
        self.sandbox = None
        self.handle = None
        self.reader = None
        self.stderr_reader = None
        self.resource = {
            "provider": self.provider,
            "resource_id": "puppyone-agent-" + execution_id,
            "session_id": execution_id,
            "project_id": project_id,
            "allocation_pending": True,
        }
        self.send_lock = asyncio.Lock()
        self.controls = {}
        self.git_handler = None
        self.git_tasks = set()
        self.recovery = None
        self.restore_point = None
        self.reconnecting = False
        self.output_sequence = 0
        self.resource["recovery_volume"] = "puppyone-agent-recovery-" + execution_id

    async def _docker(self, *args):
        proc = await asyncio.create_subprocess_exec(
            "docker", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode:
            raise RuntimeError(
                "Sandbox provider command failed: " + stderr.decode(errors="replace")[:300]
            )
        return stdout.decode().strip()

    @classmethod
    async def attach(cls, resource, project_id):
        worker = cls(resource["session_id"], project_id, provider=resource["provider"])
        worker.resource = dict(resource)
        worker.attached = True
        if resource["provider"] == "docker":
            try:
                state = json.loads(
                    await worker._docker("container", "inspect", resource["resource_id"])
                )[0]["State"]
            except RuntimeError as exc:
                if "No such container:" in str(exc) or "No such object:" in str(exc):
                    raise WorkerLost("Sandbox no longer exists") from exc
                raise
            if not state["Running"]:
                raise WorkerLost("Sandbox tmpfs no longer exists")
            if state.get("Paused"):
                await worker._docker("unpause", resource["resource_id"])
        elif resource["provider"] == "e2b":
            from e2b import AsyncSandbox
            from e2b.exceptions import NotFoundException

            resources = await cls.resolve_resources(resource)
            if len(resources) != 1:
                raise WorkerLost("No uniquely allocated sandbox workspace")
            worker.resource = resources[0]
            try:
                worker.sandbox = await AsyncSandbox.connect(
                    worker.resource["resource_id"], api_key=settings.E2B_API_KEY
                )
            except NotFoundException as exc:
                raise WorkerLost("Sandbox expired before unconfirmed files were saved") from exc
        await worker._open_control()
        return worker

    async def create(self):
        if self.restore_point:
            if (
                self.restore_point["project_id"] != self.project_id
                or self.restore_point["provider"] != self.provider
            ):
                raise PermissionError("Recovery workspace binding mismatch")
            if self.provider == "docker":
                self.resource["recovery_volume"] = self.restore_point["volume"]
        if self.provider == "docker":
            # Resolve the configured immutable artifact before creating a resource.
            inspected = json.loads(await self._docker("image", "inspect", self.image))[0]
            if self.restore_point and self.restore_point.get("artifact") != inspected["Id"]:
                raise ValueError("Recovery worker artifact mismatch")
            self.resource["image_id"] = inspected["Id"]
            await self._docker(
                "create",
                "--name",
                self.resource["resource_id"],
                "--network=none",
                "--read-only",
                "--cap-drop=ALL",
                "--cap-add=SETUID",
                "--cap-add=SETGID",
                "--cap-add=KILL",
                "--cap-add=CHOWN",
                "--cap-add=DAC_OVERRIDE",
                "--cap-add=FOWNER",
                "--security-opt=no-new-privileges",
                "--pids-limit=128",
                "--memory=768m",
                "--cpus=1",
                "--user=0:0",
                "--tmpfs=/workspace:rw,nosuid,nodev,size=256m,uid=1000,gid=1000",
                "--mount",
                "type=volume,source=" + self.resource["recovery_volume"] + ",target=/recovery",
                "--tmpfs=/tmp:rw,nosuid,nodev,size=128m,uid=1000,gid=1000",
                "--tmpfs=/home/node:rw,nosuid,nodev,size=8m,uid=1000,gid=1000",
                "-i",
                inspected["Id"],
            )
            await self._docker("start", self.resource["resource_id"])
            self.attached = True
        elif self.provider == "e2b":
            from e2b import AsyncSandbox

            if not settings.CLOUD_AGENT_E2B_TEMPLATE:
                raise RuntimeError("A pinned CLOUD_AGENT_E2B_TEMPLATE is required")
            if (
                self.restore_point
                and self.restore_point.get("artifact") != settings.CLOUD_AGENT_E2B_TEMPLATE
            ):
                raise ValueError("Recovery worker artifact mismatch")
            self.sandbox = await AsyncSandbox.create(
                template=(
                    self.restore_point["id"]
                    if self.restore_point
                    else settings.CLOUD_AGENT_E2B_TEMPLATE
                ),
                timeout=settings.RUNTIME_AGENT_TIMEOUT_SECONDS,
                lifecycle={"on_timeout": "pause", "auto_resume": False},
                allow_internet_access=False,
                metadata={"agent_execution_id": self.execution_id, "project_id": self.project_id},
                api_key=settings.E2B_API_KEY,
            )
            if self.restore_point:
                await self.sandbox.commands.run(
                    "pkill -KILL -u 1000 || true; pkill -KILL -f '^node /opt/puppyone-agent/controller.mjs$' || true",
                    user="root",
                    timeout=30,
                )
            self.resource["resource_id"] = self.sandbox.sandbox_id
            self.resource["template"] = settings.CLOUD_AGENT_E2B_TEMPLATE
        else:
            raise ValueError("Unsupported Pi sandbox provider")
        self.resource["allocation_pending"] = False
        now = time.time()
        if not self.resource.get("workspace_id"):
            self.store.put(
                ExecutionSession(
                    self.execution_id,
                    self.provider,
                    self.resource["resource_id"],
                    False,
                    now,
                    now,
                    project_id=self.project_id,
                )
            )
        return dict(self.resource)

    async def resume(self, resource):
        fresh_resource, fresh_execution = dict(self.resource), self.execution_id
        try:
            return await self._resume_resource(resource)
        except WorkerLost:
            # A cold replacement must have a NEW provider identity. A delayed
            # cleanup of the missing generation must never kill its replacement.
            self.resource, self.execution_id = fresh_resource, fresh_execution
            self.sandbox, self.attached, self.reused = None, False, False
            raise

    async def _resume_resource(self, resource):
        if (
            resource.get("project_id") != self.project_id
            or resource.get("provider") != self.provider
        ):
            raise PermissionError("Workspace resource binding mismatch")
        self.resource = dict(resource)
        self.execution_id = resource["session_id"]
        self.restore_point = None
        if self.provider == "docker":
            try:
                found = json.loads(
                    await self._docker("container", "inspect", resource["resource_id"])
                )[0]
            except RuntimeError as exc:
                if "No such container" in str(exc) or "No such object" in str(exc):
                    raise WorkerLost("Workspace was reclaimed") from exc
                raise
            artifact = json.loads(await self._docker("image", "inspect", self.image))[0]["Id"]
            if found["Image"] != artifact or not found["State"]["Running"]:
                raise WorkerLost("Workspace artifact or filesystem unavailable")
            if found["State"].get("Paused"):
                await self._docker("unpause", resource["resource_id"])
            self.attached = True
        else:
            from e2b import AsyncSandbox
            from e2b.exceptions import NotFoundException

            if resource.get("template") != settings.CLOUD_AGENT_E2B_TEMPLATE:
                raise WorkerLost("Workspace artifact changed")
            try:
                self.sandbox = await AsyncSandbox.connect(
                    resource["resource_id"],
                    timeout=settings.RUNTIME_AGENT_TIMEOUT_SECONDS,
                    api_key=settings.E2B_API_KEY,
                )
            except NotFoundException as exc:
                raise WorkerLost("Workspace was reclaimed") from exc
        self.reused = True
        return dict(self.resource)

    async def pause(self):
        # End the run's processes/relay, then park the SAME compute resource.
        # Docker PID 1 is a persistent supervisor, not this control connection.
        self.reconnecting = True
        try:
            if not self.parked:
                await self.control("park")
                self.parked = True
            if self.provider == "docker":
                await self.process.wait()
                state = json.loads(
                    await self._docker("container", "inspect", self.resource["resource_id"])
                )[0]["State"]
                if not state.get("Paused"):
                    await self._docker("pause", self.resource["resource_id"])
            else:
                await self._disconnect_output()
                await self.sandbox.pause()
            for task in (self.reader, self.stderr_reader):
                if task:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
        finally:
            self.reconnecting = False

    async def _disconnect_output(self):
        """Close the SDK subscription, not just the task awaiting its result.

        In E2B a cancelled wait does not close the underlying RPC generator.
        The previous subscription must release that stream before reconnecting.
        """
        if self.provider == "e2b" and self.handle:
            async with asyncio.timeout(SEND_TIMEOUT):
                await self.handle.disconnect()
        if self.reader:
            self.reader.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self.reader

    async def _output(self, chunk):
        self.buffer += chunk
        if len(self.buffer.encode()) > MAX_FRAME:
            raise RuntimeError("Pi control frame too large")
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            if line:
                frame = json.loads(line)
                sequence = frame.get("sequence", 0)
                if type(sequence) is not int or sequence < 1:
                    raise RuntimeError(
                        "Unsupported sandbox control protocol; rebuild worker artifact"
                    )
                if sequence <= self.output_sequence:
                    continue
                if sequence != self.output_sequence + 1:
                    raise WorkerDisconnected("Sandbox output sequence has a gap")
                self.output_sequence = sequence
                if frame.get("type") == "control_result":
                    future = self.controls.pop(frame["id"], None)
                    if future and not future.done():
                        if frame.get("error"):
                            future.set_exception(RuntimeError(frame["error"]))
                        else:
                            future.set_result(frame["result"])
                elif frame.get("type") == "git_http":
                    task = asyncio.create_task(self._git_exchange(frame))
                    self.git_tasks.add(task)
                    task.add_done_callback(self.git_tasks.discard)
                else:
                    await self.queue.put(frame)

    async def _git_exchange(self, frame):
        try:
            if self.git_handler is None:
                raise PermissionError("No admitted Git transport")
            result = await self.git_handler(frame)
            await self.send({"type": "reply", "id": frame["id"], **result})
        except Exception as exc:
            import logging

            logging.getLogger(__name__).exception("agent_git_exchange_failed")
            with suppress(Exception):
                await self.send({"type": "reply", "id": frame["id"], "error": type(exc).__name__})

    async def _disconnected(self):
        if self.reconnecting:
            return
        for future in self.controls.values():
            if not future.done():
                future.set_exception(WorkerDisconnected("Sandbox control connection closed"))
        await self.queue.put({"type": "disconnected"})

    async def control(self, action, value=None):
        phase = {"prepare": "git_prepare", "finalize": "git_commit", "push": "git_push"}.get(
            action, "sandbox_control"
        )
        with database_stage(phase):
            return await self._control(action, value)

    async def _control(self, action, value):
        if self.reader and self.reader.done() and not self.reconnecting:
            raise WorkerDisconnected("Sandbox control connection closed")
        identity = str(uuid4())
        future = asyncio.get_running_loop().create_future()
        self.controls[identity] = future
        try:
            async with asyncio.timeout(180):
                await self.send(
                    {"type": "control", "id": identity, "action": action, "value": value}
                )
                return await future
        finally:
            self.controls.pop(identity, None)

    async def _open_control(self):
        if self.provider == "docker":
            command = (
                [
                    "docker",
                    "exec",
                    "-i",
                    "--user=0:0",
                    self.resource["resource_id"],
                    "node",
                    "/opt/puppyone-agent/controller.mjs",
                ]
                if getattr(self, "attached", False)
                else ["docker", "start", "-ai", self.resource["resource_id"]]
            )
            self.process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            async def read():
                decoder = codecs.getincrementaldecoder("utf-8")()
                try:
                    while chunk := await self.process.stdout.read(65536):
                        await self._output(decoder.decode(chunk))
                    await self._output(decoder.decode(b"", final=True))
                    await self._disconnected()
                except Exception as exc:
                    await self.queue.put({"type": "failed", "error": type(exc).__name__})
                    await self._disconnected()

            async def drain_stderr():
                while await self.process.stderr.read(65536):
                    pass

            self.reader = asyncio.create_task(read())
            self.stderr_reader = asyncio.create_task(drain_stderr())
        else:
            self.handle = await self.sandbox.commands.run(
                "node /opt/puppyone-agent/controller.mjs",
                cwd="/workspace",
                user="root",
                stdin=True,
                background=True,
                timeout=0,
                on_stdout=self._output,
            )

            async def wait():
                try:
                    await self.handle.wait()
                finally:
                    await self._disconnected()

            self.reader = asyncio.create_task(wait())

    async def start(self, config):
        await self._open_control()
        capabilities = await self.control("capabilities")
        if capabilities.get("workspace_lifecycle") != 1:
            raise RuntimeError("Sandbox artifact does not support Session workspace lifecycle")
        workspace = config["workspace"]
        await self.control(
            "prepare",
            {
                **workspace,
                "project_id": self.project_id,
                "restore": self.restore_point["id"]
                if self.restore_point and self.provider == "docker"
                else None,
                "resume": bool(config.get("resume_workspace")),
                "reuse": self.reused,
            },
        )
        if config.get("resume_workspace") and self.restore_point:
            self.recovery = self.restore_point
        else:
            self.recovery = await self.snapshot()
        await self.send({"type": "start", "config": config})

    async def send(self, value):
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
        if len(data.encode()) > MAX_FRAME:
            raise ValueError("Pi input frame too large")
        try:
            async with asyncio.timeout(SEND_TIMEOUT), self.send_lock:
                if self.provider == "docker":
                    self.process.stdin.write(data.encode())
                    await self.process.stdin.drain()
                else:
                    await self.sandbox.commands.send_stdin(
                        self.handle.pid, data, request_timeout=SEND_TIMEOUT
                    )
        except TimeoutError as exc:
            raise WorkerDisconnected("Sandbox input delivery timed out") from exc

    async def receive(self):
        return await self.queue.get()

    async def touch(self):
        if self.resource.get("workspace_id"):
            if self.provider == "e2b" and self.sandbox and time.monotonic() >= self.timeout_renewal:
                await self.sandbox.set_timeout(settings.RUNTIME_AGENT_TIMEOUT_SECONDS)
                self.timeout_renewal = time.monotonic() + settings.RUNTIME_AGENT_TIMEOUT_SECONDS / 2
            return
        session = self.store.get(self.execution_id)
        if session:
            session.last_activity = time.time()
            self.store.put(session)

    async def stop(self):
        tasks = tuple(self.git_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.reconnecting = True
        await self._disconnect_output()
        await self.cleanup(self.resource, store=self.store)
        for task in (self.reader, self.stderr_reader):
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        if self.process:
            await self.process.wait()

    async def snapshot(self):
        """Provider-owned recovery. No file bytes or Git objects cross this API."""
        with database_stage("provider_snapshot"):
            return await self._snapshot()

    async def _snapshot(self):
        if self.provider == "docker":
            value = await self.control("snapshot")
            result = {
                "provider": "docker",
                "id": value["id"],
                "volume": self.resource["recovery_volume"],
                "project_id": self.project_id,
                "artifact": self.resource["image_id"],
            }
        else:
            await self.control("freeze")
            self.reconnecting = True
            try:
                await self._disconnect_output()
                # disconnect() closes the local subscription. Complete an envd
                # RPC round trip before the separate snapshot API pauses the
                # VM, so stream cancellation can reach the remote process.
                # Otherwise stale subscribers can survive the snapshot and
                # block subsequent output (including the Git push relay).
                async with asyncio.timeout(SEND_TIMEOUT):
                    await self.sandbox.commands.list(request_timeout=SEND_TIMEOUT)
                snapshot = await self.sandbox.create_snapshot()
                result = {
                    "provider": "e2b",
                    "id": snapshot.snapshot_id,
                    "project_id": self.project_id,
                    "artifact": self.resource["template"],
                }
                self.buffer = ""
                self.handle = await self.sandbox.commands.connect(
                    self.handle.pid, timeout=0, on_stdout=self._output
                )

                async def wait():
                    try:
                        await self.handle.wait()
                    finally:
                        await self._disconnected()

                self.reader = asyncio.create_task(wait())
            finally:
                self.reconnecting = False
                await self.control("thaw")
        self.recovery = result
        return dict(result)

    async def retain(self):
        """Transfer resource deletion from the idle reaper to the durable run owner."""
        await asyncio.to_thread(self.store.delete, self.execution_id)

    @staticmethod
    async def resolve_resources(resource):
        if resource["provider"] != "e2b" or not resource.get("allocation_pending"):
            return [resource]
        from e2b import AsyncSandbox
        from e2b.sandbox.sandbox_api import SandboxQuery

        # Recover create-response loss by exact execution metadata. Never list
        # or delete another execution's provider resources.
        matches = []
        pager = AsyncSandbox.list(
            query=SandboxQuery(
                metadata={
                    "agent_execution_id": resource["session_id"],
                    "project_id": resource["project_id"],
                }
            ),
            api_key=settings.E2B_API_KEY,
        )
        while pager.has_next:
            for sandbox in await pager.next_items():
                if (
                    sandbox.metadata.get("agent_execution_id") == resource["session_id"]
                    and sandbox.metadata.get("project_id") == resource["project_id"]
                ):
                    matches.append(
                        {**resource, "resource_id": sandbox.sandbox_id, "allocation_pending": False}
                    )
        return matches

    @staticmethod
    async def cleanup(resource, *, store=None):
        store = store if store is not None else durable_execution_store()
        if resource["provider"] == "docker":
            proc = await asyncio.create_subprocess_exec(
                "docker",
                "rm",
                "-f",
                resource["resource_id"],
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode and b"No such container" not in stderr:
                raise RuntimeError("Pi sandbox cleanup failed")
        else:
            from e2b import AsyncSandbox
            from e2b.exceptions import NotFoundException

            for allocated in await PiWorker.resolve_resources(resource):
                with suppress(NotFoundException):
                    await AsyncSandbox.kill(allocated["resource_id"], api_key=settings.E2B_API_KEY)
        store.delete(resource["session_id"])
