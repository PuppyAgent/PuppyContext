"""One durable run supervisor. Never launched from an HTTP response generator."""

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime

from src.config import settings
from src.platform.access.adapters.agent.runtime.checkpoints import Checkpoints, validate_files
from src.platform.access.adapters.agent.runtime.models import TERMINAL
from src.platform.access.adapters.agent.runtime.publication import PublicationRejected
from src.platform.managed_ai.contracts import InferenceFailed, ModelChunk
from src.platform.managed_ai.schemas import CompletionRequest
from src.platform.scope_sandbox.execution.pi_worker import PiWorker, WorkerLost

logger = logging.getLogger(__name__)


class EndRun(Exception):
    def __init__(self, state, code):
        self.state, self.code = state, code
        super().__init__(code)


class RunSupervisor:
    def __init__(
        self,
        repository,
        admission,
        publication,
        inference,
        billing,
        *,
        checkpoints=None,
        worker_factory=PiWorker,
        bound_tools=None,
    ):
        self.repo, self.admission, self.publication = repository, admission, publication
        self.inference, self.billing = inference, billing
        self.checkpoints = checkpoints or Checkpoints()
        self.worker_factory, self.bound_tools = worker_factory, bound_tools
        self.lock = asyncio.Lock()
        self.worker = None
        self.models = {}
        self.durable = None
        self.value = None
        self.run = None
        self.finished = False

    async def write(self, kind, payload=None, **patch):
        async with self.lock:
            self.run = await asyncio.to_thread(self.repo.write, self.run, kind, payload, **patch)
        return self.run

    async def check(self):
        current = await asyncio.to_thread(self.repo.get, self.run["id"])
        if (
            current["execution_id"] != self.run["execution_id"]
            or current["fence"] != self.run["fence"]
        ):
            raise EndRun("outcome_unknown", "execution_fenced")
        if current["stop_requested"]:
            raise EndRun("stopped", "stop_requested")
        if datetime.fromisoformat(current["deadline"].replace("Z", "+00:00")) <= datetime.now(UTC):
            raise EndRun("failed", "runtime_timeout")
        if current["billing_run_id"]:
            try:
                await self.billing.check_session(current["billing_run_id"])
            except Exception as exc:
                raise EndRun("failed", "runtime_credit_unavailable") from exc
        try:
            return await asyncio.to_thread(self.admission.recheck, self.run)
        except Exception as exc:
            raise EndRun("failed", "authorization_revoked") from exc

    async def heartbeat(self):
        while True:
            await asyncio.sleep(5)
            if self.run["state"] not in TERMINAL:
                await self.check()
            await self.write("heartbeat")
            if self.worker:
                await self.worker.touch()

    async def save(self, value, reason):
        payload = {**value, "reason": reason}
        manifest = await self.checkpoints.save(self.run, payload)
        await self.write("checkpoint", {"reason": reason}, checkpoint=manifest)
        self.value, self.durable = payload, manifest
        return manifest

    async def run_claim(self, run):
        self.run = run
        heartbeat = asyncio.create_task(self.heartbeat())
        operation = asyncio.create_task(self.execute())
        outcome = None
        try:
            done, _ = await asyncio.wait(
                {heartbeat, operation}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                await task
        except EndRun as exc:
            outcome = (exc.state, exc.code)
        except PublicationRejected:
            outcome = ("failed", "publication_rejected")
        except Exception:
            logger.exception("cloud_agent_execution_failed", extra={"run_id": run["id"]})
            outcome = ("failed", "execution_failed")
        finally:
            for task in (heartbeat, operation, *self.models.values()):
                if not task.done():
                    task.cancel()
            for task in (heartbeat, operation, *self.models.values()):
                with suppress(asyncio.CancelledError, Exception):
                    await task
        if outcome:
            current = await asyncio.to_thread(self.repo.get, run["id"])
            if (
                not current
                or current["execution_id"] != run["execution_id"]
                or current["fence"] != run["fence"]
            ):
                if self.run["billing_run_id"]:
                    await self.billing.detach_session(self.run["billing_run_id"])
                return
            self.run = current
            tools = await asyncio.to_thread(self.repo.tools, run["id"])
            if any(t["state"] == "executing" for t in tools) or (
                self.run["state"] == "publishing" and outcome[1] != "publication_rejected"
            ):
                outcome = ("outcome_unknown", outcome[1])
            if (self.run["publication"] or {}).get("status") in {"committed", "no_changes"}:
                outcome = ("succeeded", "publication_reconciled")
            elif (self.run["publication"] or {}).get("status") in {"conflict", "rejected"}:
                outcome = ("conflict", "publication_reconciled")
            elif (self.run["publication"] or {}).get("status") == "failed":
                outcome = ("failed", "publication_reconciled")
            retained = not await self.preserve_resources()
            await self.finish(outcome[0], outcome[1], retain_resource=retained)

    async def preserve_resources(self):
        """Quiesce before copying. Storage outages transfer cleanup to the run
        owner, so the generic ephemeral-session reaper cannot erase its only
        surviving files. Confirmed checkpoints remain valid if a provider died.
        """
        if self.value is None and self.run["checkpoint"]:
            self.value = await self.checkpoints.load(self.run, self.run["checkpoint"])
        saved = True
        for execution in await asyncio.to_thread(self.repo.executions, self.run["id"]):
            resource = execution["resource"]
            if not resource:
                continue
            worker = (
                self.worker
                if self.worker and self.worker.execution_id == resource["session_id"]
                else None
            )
            try:
                if self.value is None:
                    raise ValueError("Missing initial recovery checkpoint")
                worker = worker or await PiWorker.attach(resource, self.run["project_id"])
                if self.value.get("reason") != "settled":
                    if "git" in self.value:
                        workspace = await worker.capture(workspace=True)
                    else:
                        workspace = {"files": await worker.capture()}
                    await self.save({**self.value, **workspace}, "interrupted")
            except WorkerLost:
                await self.write("recovery", {"code": "unconfirmed_workspace_lost"})
            except Exception:
                saved = False
                if worker:
                    await asyncio.to_thread(worker.store.delete, resource["session_id"])
                logger.exception(
                    "cloud_agent_recovery_material_retained", extra={"run_id": self.run["id"]}
                )
        return saved

    async def execute(self):
        if self.run["state"] in TERMINAL:
            saved = await self.preserve_resources()
            await self.finish(
                self.run["state"],
                self.run["snapshot"].get("code", "interrupted"),
                retain_resource=not saved,
            )
            return
        grant = await self.check()
        if self.run["publication"] and self.run["publication"].get("status") in {
            "committed",
            "no_changes",
        }:
            await self.finish("succeeded", "publication_reconciled")
            return
        publication_status = (self.run["publication"] or {}).get("status")
        if publication_status in {"conflict", "rejected", "failed"}:
            await self.finish(
                "failed" if publication_status == "failed" else "conflict", "publication_reconciled"
            )
            return
        tools = await asyncio.to_thread(self.repo.tools, self.run["id"])
        if any(tool["state"] == "executing" for tool in tools):
            raise EndRun("outcome_unknown", "tool_outcome_unknown")
        if self.run["state"] == "publishing":
            raise EndRun("outcome_unknown", "publication_outcome_unknown")
        # Takeover first quiesces old providers; no old process is reattached as
        # an execution owner. All confirmed state lives outside that provider.
        for execution in await asyncio.to_thread(self.repo.executions, self.run["id"]):
            if execution["id"] != self.run["execution_id"] and execution["resource"]:
                # With no executing tool, the last checkpoint/receipt is the
                # complete acknowledged state. Quiesce the old process before
                # cleanup; a late file mutation cannot join the new execution.
                await PiWorker.cleanup(execution["resource"])
                await asyncio.to_thread(self.repo.cleaned, execution["id"])
        resume = bool(self.run["checkpoint"])
        if resume:
            self.value = await self.checkpoints.load(self.run, self.run["checkpoint"])
            # A completed receipt may have committed just before its checkpoint
            # manifest update. Its immutable object includes the resulting files.
            present = {
                e.get("message", {}).get("toolCallId") for e in self.value.get("entries") or []
            }
            for tool in tools:
                if tool["state"] == "completed" and tool["call_id"] not in present:
                    self.value = await self.checkpoints.load(self.run, tool["result"]["checkpoint"])
            if self.value["reason"] == "settled":
                await self.publish(grant)
                return
            if self.value["reason"] == "model_failed":
                raise EndRun("failed", "model_failed")
            # A model stream can die after partial UI text but before its Pi
            # message is acknowledged. Rebuild this run's visible text from
            # durable entries before continuing, avoiding duplicated prefixes.
            confirmed = "".join(
                part.get("text", "")
                for entry in (self.value.get("entries") or [])[
                    self.value.get("run_entry_start", 0) :
                ]
                if entry.get("message", {}).get("role") == "assistant"
                for part in entry["message"].get("content", [])
                if part.get("type") == "text"
            )[-200000:]
            await self.write(
                "text_reset",
                {"text": confirmed},
                snapshot={**self.run["snapshot"], "text": confirmed},
            )
        else:
            self.value = await asyncio.to_thread(self.publication.capture, self.run, grant)
            previous = await asyncio.to_thread(self.repo.previous, self.run)
            if previous and previous["checkpoint"]:
                if previous["state"] not in {"succeeded", "stopped", "failed"} or (
                    previous["publication"] or {}
                ).get("status") not in {"committed", "no_changes"}:
                    raise EndRun("failed", "previous_run_requires_resolution")
                old = await self.checkpoints.load(previous, previous["checkpoint"])
                self.value.update(entries=old["entries"], leaf_id=old["leaf_id"])
            self.value["run_entry_start"] = len(self.value.get("entries") or [])
            await self.save(self.value, "prepared")
        if self.run["billing_run_id"]:
            billing_id = await self.billing.resume_session(
                self.run["billing_run_id"],
                project_id=self.run["project_id"],
                actor_id=self.run["user_id"],
            )
        else:
            billing_id = await self.billing.start_session(
                audit_context={
                    "source": "chat_agent",
                    "run_id": "cloud-agent:" + self.run["id"],
                    "project_id": self.run["project_id"],
                    "user_id": self.run["user_id"],
                    "maximum_runtime_units": settings.RUNTIME_AGENT_MAX_UNITS,
                }
            )
        await self.write("state", {"state": "running"}, state="running", billing_run_id=billing_id)
        self.worker = self.worker_factory(self.run["execution_id"], self.run["project_id"])
        # Provider identity intent is durable before allocation (Docker's name is
        # deterministic; E2B also carries the execution identity in metadata).
        await self.write("resource", {}, resource=self.worker.resource)
        resource = await self.worker.create()
        await self.write("resource", {}, resource=resource)
        definitions = (
            await asyncio.to_thread(self.bound_tools.definitions, self.run)
            if self.bound_tools
            else []
        )
        await self.worker.start(
            {
                **self.run["policy"],
                **{
                    key: self.value[key]
                    for key in ("version", "pi_version", "entries", "leaf_id", "files")
                },
                **{key: self.value[key] for key in ("git", "modes") if key in self.value},
                "prompt": self.run["prompt"],
                "finalize_only": self.value.get("agent_settled", False),
                "resume": resume and self.value["reason"] != "prepared",
                "tools": definitions,
                "receipts": tools,
            }
        )
        await self.consume()
        await self.publish(await self.check())

    async def consume(self):
        while True:
            frame = await self.worker.receive()
            kind = frame.get("type")
            if kind == "text":
                delta = str(frame["delta"])
                snapshot = {
                    **self.run["snapshot"],
                    "text": (self.run["snapshot"].get("text", "") + delta)[-200000:],
                }
                await self.write("text", {"delta": delta[:100000]}, snapshot=snapshot)
            elif kind == "checkpoint":
                supplied = frame["checkpoint"]
                if "git" in self.value and not isinstance(supplied.get("git"), dict):
                    raise ValueError("Sandbox artifact did not checkpoint Git state")
                value = {
                    key: supplied[key]
                    for key in ("version", "pi_version", "entries", "leaf_id", "files")
                }
                value.update({key: supplied[key] for key in ("git", "modes") if key in supplied})
                reason = "agent_settled" if frame["reason"] == "settled" else frame["reason"]
                if reason == "agent_settled":
                    value["agent_settled"] = True
                await self.save({**self.value, **value}, reason)
                await self.worker.send({"type": "reply", "id": frame["id"]})
            elif kind == "tool_start":
                await self.tool_start(frame)
            elif kind == "tool_end":
                await self.tool_end(frame)
            elif kind == "bound_tool":
                await self.bound_tool(frame)
            elif kind == "model_request":
                await self.check()
                self.models[frame["id"]] = asyncio.create_task(self.model(frame))
            elif kind == "model_cancel":
                if task := self.models.get(frame["id"]):
                    task.cancel()
            elif kind == "finished":
                if frame.get("stopped"):
                    raise EndRun("stopped", "stop_requested")
                if frame.get("error"):
                    raise EndRun("failed", "model_failed")
                self.finished = True
                await self.check()
                if "git" in self.value:
                    workspace = await self.worker.capture(
                        workspace=True,
                        commit=None
                        if self.run["policy"]["readonly"]
                        else "Agent run " + self.run["id"],
                    )
                else:
                    workspace = {"files": await self.worker.capture()}
                await self.save({**self.value, **workspace}, "settled")
                return
            elif kind in {"failed", "disconnected"}:
                raise EndRun("failed", "worker_disconnected")
            elif kind == "ready":
                if frame.get("pi_version") != "0.85.1" or frame.get("node_version") != "v22.22.3":
                    raise ValueError("Unexpected worker artifact")
                if "git" in self.value and frame.get("workspace_version") != 1:
                    raise ValueError("Sandbox artifact does not support Git workspaces")
                await self.write(
                    "ready", {key: frame[key] for key in ("pi_version", "node_version")}
                )
            else:
                raise ValueError("Unknown worker frame")

    async def model(self, frame):
        request_id = frame["id"]
        inference = None
        error = None
        try:
            if not settings.MANAGED_AI_ENABLED:
                raise PermissionError("Managed Agent inference is disabled")
            body = CompletionRequest.model_validate(frame["request"])
            count = self.run["snapshot"].get("model_calls", 0) + 1
            if (
                count > settings.CLOUD_AGENT_MAX_MODEL_CALLS
                or body.model != self.run["policy"]["model"]
                or body.max_tokens > self.run["policy"]["max_tokens"]
            ):
                raise PermissionError("Model request exceeds run policy")
            await self.write(
                "model",
                {"request_id": request_id},
                snapshot={**self.run["snapshot"], "model_calls": count},
            )
            inference = await self.inference.completion(
                self.run["user_id"], "cloud-agent:" + request_id, body
            )
            async for event in inference.events:
                if isinstance(event, ModelChunk):
                    await self.worker.send(
                        {"type": "model_chunk", "id": request_id, "frame": event.frame}
                    )
                elif isinstance(event, InferenceFailed):
                    error = event.code
        except Exception:
            logger.exception("cloud_agent_model_failed", extra={"run_id": self.run["id"]})
            error = "model_request_failed"
        finally:
            if inference:
                await inference.aclose()
            with suppress(Exception):
                await self.worker.send({"type": "model_end", "id": request_id, "error": error})

    async def tool_start(self, frame):
        await self.check()
        name = frame["name"]
        allowed = {"read", "ls", "find", "grep"}
        if not self.run["policy"]["readonly"]:
            allowed |= {"write", "edit", "bash"}
        if name not in allowed:
            raise PermissionError("Tool outside run policy")
        mutation = name in {"write", "edit", "bash"}
        if name == "bash":
            from src.platform.scope_sandbox.execution_policy import assert_command_allowed

            assert_command_allowed(frame["input"].get("command", ""))
        receipt = await asyncio.to_thread(
            self.repo.tool, self.run, frame, "waiting" if mutation else "executing"
        )
        if receipt["state"] == "completed":
            await self.worker.send(
                {"type": "reply", "id": frame["id"], "result": receipt["result"]["pi_result"]}
            )
            return
        if receipt["state"] == "waiting":
            await self.write("state", {"state": "waiting_approval"}, state="waiting_approval")
            while receipt["state"] == "waiting":
                await asyncio.sleep(0.5)
                await self.check()
                receipts = await asyncio.to_thread(self.repo.tools, self.run["id"])
                receipt = next(t for t in receipts if t["call_id"] == frame["call_id"])
        if receipt["state"] == "approved":
            await asyncio.to_thread(self.repo.tool, self.run, frame, "executing")
        await self.write("state", {"state": "running"}, state="running")
        await self.worker.send(
            {"type": "reply", "id": frame["id"], "allow": receipt["state"] != "rejected"}
        )

    async def tool_end(self, frame):
        if "git" in self.value and not isinstance(frame.get("git"), dict):
            raise ValueError("Missing tool Git checkpoint")
        validate_files(frame["files"])
        value = {**self.value, "files": frame["files"], "reason": "after_tool"}
        value.update({key: frame[key] for key in ("git", "modes") if key in frame})
        manifest = await self.checkpoints.save(self.run, value)
        await asyncio.to_thread(
            self.repo.tool,
            self.run,
            frame,
            "completed",
            {"pi_result": frame["result"], "checkpoint": manifest},
        )
        await self.write("checkpoint", {"reason": "after_tool"}, checkpoint=manifest)
        self.value, self.durable = value, manifest
        await self.worker.send({"type": "reply", "id": frame["id"]})

    async def bound_tool(self, frame):
        if self.bound_tools is None:
            raise PermissionError("No configured tools")
        await self.check()
        receipt = await asyncio.to_thread(self.repo.tool, self.run, frame, "executing")
        if receipt["state"] == "completed":
            result = receipt["result"]["pi_result"]
        else:
            result = await self.bound_tools.execute(self.run, frame["name"], frame["input"])
            await asyncio.to_thread(
                self.repo.tool,
                self.run,
                frame,
                "completed",
                {"pi_result": result, "checkpoint": self.run["checkpoint"]},
            )
        await self.worker.send({"type": "reply", "id": frame["id"], "result": result})

    async def publish(self, grant):
        await self.write("state", {"state": "publishing"}, state="publishing")
        result = await self.publication.publish(self.run, self.value, grant)
        await self.write("publication", result, publication=result)
        await self.finish(
            "succeeded" if result["status"] in {"committed", "no_changes"} else "conflict",
            "completed",
        )

    async def finish(self, state, code, *, retain_resource=False):
        # Cleanup happens before terminal ACK and is retried by takeover on a
        # crash. Provider deletion is safe only once recovery data is durable.
        await self.write("heartbeat")
        if not retain_resource:
            for execution in await asyncio.to_thread(self.repo.executions, self.run["id"]):
                if execution["fence"] > self.run["fence"]:
                    continue
                if execution["resource"]:
                    if (
                        self.worker
                        and execution["resource"]["session_id"] == self.worker.execution_id
                    ):
                        await self.worker.stop()
                    else:
                        await PiWorker.cleanup(execution["resource"])
                await asyncio.to_thread(self.repo.cleaned, execution["id"])
        if self.run["billing_run_id"] and not retain_resource:
            await self.billing.finish_session(self.run["billing_run_id"])
        snapshot = {**self.run["snapshot"], "code": code, "resource_retained": retain_resource}
        await self.write(
            "terminal",
            {"state": state, "code": code, "resource_retained": retain_resource},
            state=state,
            snapshot=snapshot,
        )
