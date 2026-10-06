"""Pi-specific process transport using the existing sandbox provider registry.

No host mounts, model keys or shell interpolation of project data. Providers
expose the same framed channel. This module does not own Agent run state.
"""

import asyncio
import codecs
import json
import time
from contextlib import suppress

from src.config import settings
from src.platform.scope_sandbox.execution.store import ExecutionSession, durable_execution_store

MAX_FRAME = 96 * 1024 * 1024


class WorkerLost(RuntimeError):
    """Provider no longer has an unconfirmed filesystem to preserve."""


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
        if resource["provider"] == "e2b":
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
        return worker

    async def create(self):
        if self.provider == "docker":
            # Resolve the configured immutable artifact before creating a resource.
            inspected = json.loads(await self._docker("image", "inspect", self.image))[0]
            self.resource["image_id"] = inspected["Id"]
            await self._docker(
                "create",
                "--name",
                self.resource["resource_id"],
                "--network=none",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--pids-limit=128",
                "--memory=768m",
                "--cpus=1",
                "--user=1000:1000",
                "--tmpfs=/workspace:rw,nosuid,nodev,size=128m,uid=1000,gid=1000",
                "--tmpfs=/tmp:rw,nosuid,nodev,size=128m,uid=1000,gid=1000",
                "--tmpfs=/home/node:rw,nosuid,nodev,size=8m,uid=1000,gid=1000",
                "-i",
                inspected["Id"],
            )
        elif self.provider == "e2b":
            from e2b import AsyncSandbox

            if not settings.CLOUD_AGENT_E2B_TEMPLATE:
                raise RuntimeError("A pinned CLOUD_AGENT_E2B_TEMPLATE is required")
            self.sandbox = await AsyncSandbox.create(
                template=settings.CLOUD_AGENT_E2B_TEMPLATE,
                timeout=settings.RUNTIME_AGENT_TIMEOUT_SECONDS,
                allow_internet_access=False,
                metadata={"agent_execution_id": self.execution_id, "project_id": self.project_id},
                api_key=settings.E2B_API_KEY,
            )
            self.resource["resource_id"] = self.sandbox.sandbox_id
            self.resource["template"] = settings.CLOUD_AGENT_E2B_TEMPLATE
        else:
            raise ValueError("Unsupported Pi sandbox provider")
        self.resource["allocation_pending"] = False
        now = time.time()
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

    async def _output(self, chunk):
        self.buffer += chunk
        if len(self.buffer.encode()) > MAX_FRAME:
            raise RuntimeError("Pi control frame too large")
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            if line:
                await self.queue.put(json.loads(line))

    async def start(self, config):
        if self.provider == "docker":
            self.process = await asyncio.create_subprocess_exec(
                "docker",
                "start",
                "-ai",
                self.resource["resource_id"],
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
                    await self.queue.put({"type": "disconnected"})
                except Exception as exc:
                    await self.queue.put({"type": "failed", "error": type(exc).__name__})

            async def drain_stderr():
                while await self.process.stderr.read(65536):
                    pass

            self.reader = asyncio.create_task(read())
            self.stderr_reader = asyncio.create_task(drain_stderr())
        else:
            self.handle = await self.sandbox.commands.run(
                "node /opt/puppyone-agent/worker.mjs",
                cwd="/workspace",
                user="node",
                stdin=True,
                background=True,
                timeout=0,
                on_stdout=self._output,
            )

            async def wait():
                try:
                    await self.handle.wait()
                finally:
                    await self.queue.put({"type": "disconnected"})

            self.reader = asyncio.create_task(wait())
        await self.send({"type": "start", "config": config})

    async def send(self, value):
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
        if len(data.encode()) > MAX_FRAME:
            raise ValueError("Pi input frame too large")
        async with self.send_lock:
            if self.provider == "docker":
                self.process.stdin.write(data.encode())
                await self.process.stdin.drain()
            else:
                await self.sandbox.commands.send_stdin(self.handle.pid, data)

    async def receive(self):
        return await self.queue.get()

    async def touch(self):
        session = self.store.get(self.execution_id)
        if session:
            session.last_activity = time.time()
            self.store.put(session)

    async def stop(self):
        await self.cleanup(self.resource, store=self.store)
        for task in (self.reader, self.stderr_reader):
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        if self.process:
            await self.process.wait()

    async def capture(self):
        """Freeze all tool processes, read the actual mounted filesystem, then
        leave it frozen until its immutable checkpoint permits cleanup.

        Docker's archive API cannot read tmpfs contents. A trusted image helper
        reads them inside the mount namespace; no archive is extracted on host.
        """
        if self.provider == "docker":
            try:
                state = json.loads(
                    await self._docker(
                        "inspect", "--format", "{{json .State}}", self.resource["resource_id"]
                    )
                )
            except RuntimeError as exc:
                if "No such" in str(exc):
                    raise WorkerLost("Sandbox resource no longer exists") from exc
                raise
            if not state["Running"]:
                raise WorkerLost("Sandbox exited before unconfirmed files were saved")
            await self._docker("kill", "--signal=STOP", self.resource["resource_id"])
            proc = await asyncio.create_subprocess_exec(
                "docker",
                "exec",
                "--user=1000:1000",
                self.resource["resource_id"],
                "node",
                "/opt/puppyone-agent/capture.mjs",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            output = bytearray()
            while chunk := await proc.stdout.read(65536):
                output.extend(chunk)
                if len(output) > MAX_FRAME:
                    proc.kill()
                    await proc.wait()
                    raise ValueError("Recovery output exceeds limit")
            _, error = await proc.communicate()
            if proc.returncode:
                raise RuntimeError(
                    "Cannot capture interrupted workspace: " + error.decode(errors="replace")[:300]
                )
        else:
            result = await self.sandbox.commands.run(
                "node /opt/puppyone-agent/capture.mjs", user="node", timeout=30
            )
            if result.exit_code:
                raise RuntimeError("Cannot capture interrupted workspace")
            output = result.stdout
        files = json.loads(output)
        from src.platform.access.adapters.agent.runtime.checkpoints import validate_files

        return validate_files(files)

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
