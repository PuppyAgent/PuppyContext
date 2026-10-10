"""One durable run supervisor. Never launched from an HTTP response generator."""

import asyncio
import logging
import time
from contextlib import suppress
from uuid import uuid4

from src.config import settings
from src.infra.supabase.instrumentation import DatabaseTrace, database_stage, database_trace
from src.platform.access.adapters.agent.runtime.checkpoints import Checkpoints
from src.platform.access.adapters.agent.runtime.heartbeat import EndRun, ExecutionLease
from src.platform.access.adapters.agent.runtime.model_progress import ModelProgress
from src.platform.access.adapters.agent.runtime.models import TERMINAL
from src.platform.access.adapters.agent.runtime.ports import RunStore
from src.platform.access.adapters.agent.runtime.publication import PublicationRejected
from src.platform.access.adapters.agent.runtime.recovery import Recovery
from src.platform.access.adapters.agent.runtime.tool_policy import approval_reason
from src.platform.access.adapters.agent.runtime.tool_results import (
    approval_denied,
    tool_denial,
    tool_error,
)
from src.platform.managed_ai.contracts import InferenceFailed, ModelChunk
from src.platform.managed_ai.schemas import CompletionRequest
from src.platform.scope_sandbox.execution.worker_port import WorkerLifecycle, WorkerLost

logger = logging.getLogger(__name__)


class RunSupervisor:
    def __init__(
        self,
        repository: RunStore,
        admission,
        publication,
        inference,
        billing,
        *,
        checkpoints=None,
        worker_factory,
        worker_lifecycle: WorkerLifecycle,
        bound_tools=None,
        workspace=None,
    ):
        self.repo, self.admission, self.publication = repository, admission, publication
        self.inference, self.billing = inference, billing
        self.checkpoints = checkpoints or Checkpoints()
        self.recovery = Recovery()
        self.worker_factory, self.bound_tools = worker_factory, bound_tools
        self.worker_lifecycle = worker_lifecycle
        self.workspace = workspace
        self.lock = asyncio.Lock()
        self.worker = None
        self.models = {}
        self.model_progress = ModelProgress()
        self.durable = None
        self.value = None
        self.run = None
        self.finished = False
        self.lease = ExecutionLease(repository)
        self.context = None
        self.execution = None
        self.pending_text = ""
        self.pending_batch = None
        self.metrics = None
        self.started_at = None
        self.first_text_seconds = None
        self.elapsed_seconds = None
        self.worker_started_at = None
        self.tool_started_at = {}

    async def write(self, kind, payload=None, **patch):
        async with self.lock:
            self.run = await asyncio.to_thread(self.repo.write, self.run, kind, payload, **patch)
        return self.run

    async def command(self, method, *args, **kwargs):
        async with self.lock:
            reply = await asyncio.to_thread(method, self.run, *args, **kwargs)
            self.run = reply["run"]
        status = reply.get("status", {})
        if code := status.get("code"):
            raise EndRun("stopped" if code == "stop_requested" else "failed", code)
        await self.check_billing(self.run["billing_run_id"])
        return reply

    async def conversation_manifest(self, supplied, reason):
        value = {
            **self.value,
            **{key: supplied[key] for key in ("version", "pi_version", "entries", "leaf_id")},
            "reason": reason,
        }
        return value, await self.checkpoints.save(self.run, value)

    async def check(self):
        status = await self.lease.check(self.run)
        await self.check_billing(status["billing_run_id"])
        return self.context.grant

    async def check_billing(self, billing_run_id):
        if billing_run_id:
            try:
                await self.billing.check_session(billing_run_id)
            except Exception as exc:
                raise EndRun("failed", "runtime_credit_unavailable") from exc

    async def heartbeat(self):
        async def touch():
            if self.run["state"] not in TERMINAL:
                await self.check_billing(self.run["billing_run_id"])
            if self.worker:
                await self.worker.touch()

        await self.lease.run(lambda: self.run, touch)

    async def flush_text(self):
        async with self.lock:
            if not self.pending_text:
                return
            while self.pending_text:
                if self.pending_batch is None:
                    delta = self.pending_text[:16000]
                    snapshot = {
                        **self.run["snapshot"],
                        "text": (self.run["snapshot"].get("text", "") + delta)[-200000:],
                    }
                    self.pending_batch = (
                        str(uuid4()),
                        len(delta),
                        [
                            {
                                "kind": "text",
                                "payload": {"delta": delta},
                                "patch": {"snapshot": snapshot},
                            }
                        ],
                    )
                batch_id, length, events = self.pending_batch
                self.run = await asyncio.to_thread(
                    self.repo.append_events_batch,
                    self.run,
                    events,
                    batch_id=batch_id,
                )
                self.pending_text = self.pending_text[length:]
                self.pending_batch = None

    async def text_flush_loop(self):
        while True:
            await asyncio.sleep(1)
            await self.flush_text()

    async def watch_models(self):
        while True:
            self.model_progress.check()
            for identity, task in list(self.models.items()):
                if task.done():
                    del self.models[identity]
                    if not task.cancelled():
                        await task  # Transport/cleanup failures must reach the supervisor.
            await asyncio.sleep(1)

    async def save(self, value, reason, **patch):
        await self.flush_text()
        payload = {**value, "reason": reason}
        with database_stage("checkpoint"):
            manifest = await self.checkpoints.save(self.run, payload)
            if reason == "agent_settled":
                await self.command(self.repo.settle_model, checkpoint=manifest)
            else:
                await self.write("checkpoint", {"reason": reason}, checkpoint=manifest, **patch)
        self.value, self.durable = payload, manifest
        return manifest

    async def run_claim(self, run):
        self.metrics = DatabaseTrace(run["id"])
        self.started_at = time.monotonic()
        with database_trace(self.metrics):
            try:
                await self._run_claim(run)
            finally:
                self.elapsed_seconds = time.monotonic() - self.started_at
                logger.info(
                    "cloud_agent_performance",
                    extra={
                        "agent_performance": {
                            **self.metrics.report(),
                            "elapsed_seconds": time.monotonic() - self.started_at,
                            "first_text_seconds": self.first_text_seconds,
                        }
                    },
                )

    async def _run_claim(self, run):
        self.run = run
        heartbeat = asyncio.create_task(self.heartbeat())
        operation = asyncio.create_task(self.execute())
        flusher = asyncio.create_task(self.text_flush_loop())
        models = asyncio.create_task(self.watch_models())
        outcome = None
        try:
            done, _ = await asyncio.wait(
                {heartbeat, operation, flusher, models}, return_when=asyncio.FIRST_COMPLETED
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
            for task in (heartbeat, operation, flusher, models, *self.models.values()):
                if not task.done():
                    task.cancel()
            for task in (heartbeat, operation, flusher, models, *self.models.values()):
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
                if (
                    self.worker
                    and self.worker.execution_id == resource["session_id"]
                    and not self.worker.resource.get("allocation_pending")
                )
                else None
            )
            try:
                if self.value is None:
                    raise ValueError("Missing initial recovery checkpoint")
                worker = worker or await self.worker_lifecycle.attach(
                    resource, self.run["project_id"]
                )
                if self.value.get("reason") != "settled":
                    await worker.control("stop")
                    workspace = self.value["workspace"]
                    # Recover a commit created before the manifest ACK was lost.
                    # Damaged metadata must not prevent preservation of the files.
                    with suppress(Exception):
                        actual = await worker.control("inspect", workspace)
                        workspace = {
                            **workspace,
                            **actual,
                            "changed": actual["tip"] != workspace["base_oid"],
                        }
                    recovery = await worker.snapshot()
                    await self.save(
                        {**self.value, "workspace": workspace, "recovery": recovery}, "interrupted"
                    )
            except WorkerLost:
                await self.write("recovery", {"code": "unconfirmed_workspace_lost"})
            except Exception:
                saved = False
                if worker:
                    await worker.retain()
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
        self.execution = self.run.get("_execution") or await asyncio.to_thread(
            self.repo.load_execution, self.run["id"]
        )
        self.context = self.admission.evaluate(
            self.execution["context"], self.run["user_id"], self.run["project_id"]
        )
        grant = self.context.grant
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
        tools = self.execution["tools"]
        if any(tool["state"] == "executing" for tool in tools):
            raise EndRun("outcome_unknown", "tool_outcome_unknown")
        if self.run["state"] == "publishing":
            saved = await self.checkpoints.load(self.run, self.run["checkpoint"])
            if saved.get("reason") != "settled" or not saved.get("recovery"):
                raise EndRun("outcome_unknown", "publication_outcome_unknown")
            # A durable, quiesced candidate can be reconciled by the original
            # operation key. Never rerun the model or invent a new commit.

        # Takeover first quiesces old providers; no old process is reattached as
        # an execution owner. All confirmed state lives outside that provider.
        for execution in self.execution["executions"]:
            if execution["id"] != self.run["execution_id"] and execution["resource"]:
                # With no executing tool, the last checkpoint/receipt is the
                # complete acknowledged state. Quiesce the old process before
                # cleanup; a late file mutation cannot join the new execution.
                await self.worker_lifecycle.cleanup(execution["resource"])
                await asyncio.to_thread(self.repo.cleaned, execution["id"])
        resume = bool(self.run["checkpoint"])
        if resume:
            self.value = await self.checkpoints.load(self.run, self.run["checkpoint"])
            # A completed receipt may have committed just before the current
            # manifest update. Its provider reference includes resulting files.
            present = {
                e.get("message", {}).get("toolCallId") for e in self.value.get("entries") or []
            }
            for tool in tools:
                if tool["state"] == "completed" and tool["call_id"] not in present:
                    self.value = await self.checkpoints.load(self.run, tool["result"]["checkpoint"])
            if self.value["reason"] == "settled":
                self.value["agent_settled"] = True
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
            with database_stage("workspace_prepare"):
                self.value = await asyncio.to_thread(self.publication.capture, self.run, grant)
            previous = self.execution["previous"]
            if previous and previous["checkpoint"]:
                if previous["state"] not in {"succeeded", "stopped", "failed"} or (
                    previous["publication"] or {}
                ).get("status") not in {"committed", "no_changes"}:
                    raise EndRun("failed", "previous_run_requires_resolution")
                old = await self.checkpoints.load(previous, previous["checkpoint"])
                self.value.update(
                    entries=old["entries"], leaf_id=old["leaf_id"], recovery=old.get("recovery")
                )
            self.value["run_entry_start"] = len(self.value.get("entries") or [])
            self.durable = await self.checkpoints.save(self.run, self.value)
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
        self.worker = self.worker_factory(self.run["execution_id"], self.run["project_id"])
        self.worker.restore_point = self.value.get("recovery")

        async def git_exchange(frame):
            # Git reads check current Project authority in their own SQL read;
            # receive-pack additionally checks the Run fence through its lease.
            # Long transfers remain cancellable by the periodic execution lease.
            current_grant = self.context.grant
            stage = (
                "git_receive_http"
                if "git-receive-pack" in frame.get("path", "")
                else "git_fetch_http"
            )
            with database_stage(stage):
                return await self.publication.exchange(self.run, self.value, current_grant, frame)

        self.worker.git_handler = git_exchange
        # Provider identity intent is durable before allocation (Docker's name is
        # deterministic; E2B also carries the execution identity in metadata).
        await self.command(
            self.repo.start_execution,
            checkpoint=self.durable or self.run["checkpoint"],
            billing=billing_id,
            resource=self.worker.resource,
        )
        with database_stage("sandbox_acquire"):
            resource = (
                await self.workspace.acquire(self.run, self.value, self.worker)
                if self.workspace
                else await self.worker.create()
            )
        await self.write("resource", {}, resource=resource)
        definitions = (
            await asyncio.to_thread(self.bound_tools.definitions, self.run)
            if self.bound_tools
            else []
        )
        self.worker_started_at = time.monotonic()
        await self.worker.start(
            {
                **self.run["policy"],
                **{key: self.value[key] for key in ("version", "pi_version", "entries", "leaf_id")},
                "workspace": self.value["workspace"],
                "resume_workspace": resume and bool(self.value.get("recovery")),
                "prompt": self.run["prompt"],
                "finalize_only": self.value.get("agent_settled", False),
                "resume": resume and self.value["reason"] != "prepared",
                "tools": definitions,
                "receipts": tools,
            }
        )
        await self.consume()
        await self.publish(self.context.grant)

    async def consume(self):
        while True:
            frame = await self.worker.receive()
            kind = frame.get("type")
            if kind in {
                "checkpoint",
                "tool_start",
                "bound_tool",
                "finished",
                "failed",
                "disconnected",
            }:
                self.model_progress.acknowledged()
            if kind != "text":
                await self.flush_text()
            if kind == "text":
                if self.first_text_seconds is None:
                    self.first_text_seconds = time.monotonic() - self.started_at
                self.pending_text += str(frame["delta"])
                if len(self.pending_text.encode()) >= 32768:
                    await self.flush_text()
            elif kind == "checkpoint":
                supplied = frame["checkpoint"]
                value = {
                    key: supplied[key] for key in ("version", "pi_version", "entries", "leaf_id")
                }
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
                self.model_progress.started(frame["id"])
                self.models[frame["id"]] = asyncio.create_task(self.model(frame))
            elif kind == "model_cancel":
                if task := self.models.get(frame["id"]):
                    task.cancel()
            elif kind == "finished":
                if frame.get("stopped"):
                    raise EndRun("stopped", "stop_requested")
                if frame.get("error"):
                    raise getattr(self, "model_rejection", EndRun("failed", "model_failed"))
                self.finished = True
                with database_stage("sandbox_capture_commit"):
                    self.value = await self.recovery.finalize(self.worker, self.value, self.run)
                await self.save(self.value, "settled", state="publishing")
                return
            elif kind in {"failed", "disconnected"}:
                raise EndRun("failed", "worker_disconnected")
            elif kind == "ready":
                if self.worker_started_at is not None:
                    self.metrics.duration(
                        "sandbox_restore_and_start", time.monotonic() - self.worker_started_at
                    )
                    self.worker_started_at = None
                if frame.get("pi_version") != "0.85.1" or frame.get("node_version") != "v22.22.3":
                    raise ValueError("Unexpected worker artifact")
                if frame.get("workspace_version") != 2:
                    raise ValueError("Sandbox artifact does not support provider workspaces")
                if frame.get("operation_version") != 1:
                    raise EndRun("failed", "worker_protocol_mismatch")
                self.value["recovery"] = self.worker.recovery
            else:
                raise ValueError("Unknown worker frame")

    async def model(self, frame):
        with database_stage("model_stream"):
            await self._model(frame)

    async def _model(self, frame):
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
            value, manifest = await self.conversation_manifest(frame["checkpoint"], "before_model")
            await self.command(
                self.repo.begin_model,
                request=request_id,
                checkpoint=manifest,
                limit=settings.CLOUD_AGENT_MAX_MODEL_CALLS,
            )
            self.value, self.durable = value, manifest
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
        except EndRun as exc:
            self.model_rejection = exc
            error = exc.code
        except Exception:
            logger.exception("cloud_agent_model_failed", extra={"run_id": self.run["id"]})
            error = "model_request_failed"
        finally:
            try:
                if inference:
                    async with asyncio.timeout(10):
                        await inference.aclose()
            finally:
                await self.worker.send({"type": "model_end", "id": request_id, "error": error})
                self.model_progress.delivered(request_id)

    async def tool_start(self, frame):
        name = frame["name"]
        denial = tool_denial(name, self.run["policy"]["readonly"])
        if denial is None and frame.get("error_result") is not None:
            message = "\n".join(
                str(part.get("text", ""))
                for part in frame["error_result"].get("content", [])
                if part.get("type") == "text"
            )[:10000]
            denial = tool_error(
                "tool_invalid_input", message or "The SDK could not execute this tool call."
            )
        # Approval and mutation recovery are separate: ordinary sandbox writes
        # execute automatically and still receive durable receipts/snapshots.
        approval_required = denial is None and approval_reason(name, frame["input"]) is not None
        value, manifest = await self.conversation_manifest(frame["checkpoint"], "before_tool")
        reply = await self.command(
            self.repo.begin_tool, frame, checkpoint=manifest, approval_required=approval_required
        )
        receipt = reply["tool"]
        if receipt["state"] == "completed":
            await self.worker.send(
                {"type": "reply", "id": frame["id"], "result": receipt["result"]["pi_result"]}
            )
            return
        self.value, self.durable = value, manifest
        if denial is not None:
            await self.command(
                self.repo.complete_tool,
                frame,
                result={"pi_result": denial, "checkpoint": manifest},
                checkpoint=manifest,
            )
            await self.worker.send({"type": "reply", "id": frame["id"], "result": denial})
            return
        while receipt["state"] == "waiting":
            await asyncio.sleep(1)
            reply = await self.command(
                self.repo.begin_tool, frame, checkpoint=manifest, approval_required=approval_required
            )
            receipt = reply["tool"]
        if receipt["state"] == "rejected":
            # The rejected receipt and original input are already durable.
            # Preserve its decision identity rather than pretending it executed.
            await self.command(self.repo.continue_after_rejection, frame)
            await self.worker.send(
                {"type": "reply", "id": frame["id"], "result": approval_denied()}
            )
            return
        await self.worker.send(
            {"type": "reply", "id": frame["id"], "allow": receipt["state"] != "rejected"}
        )
        self.tool_started_at[frame["call_id"]] = time.monotonic()

    async def tool_end(self, frame):
        started = self.tool_started_at.pop(frame["call_id"], None)
        if started is not None and self.metrics:
            self.metrics.duration("sandbox_tools", time.monotonic() - started)
        value = await self.recovery.after_tool(self.worker, self.value, frame)
        manifest = await self.checkpoints.save(self.run, value)
        await self.command(
            self.repo.complete_tool,
            frame,
            result={"pi_result": frame["result"], "checkpoint": manifest},
            checkpoint=manifest,
        )
        self.value, self.durable = value, manifest
        await self.worker.send({"type": "reply", "id": frame["id"]})

    async def bound_tool(self, frame):
        if self.bound_tools is None:
            raise PermissionError("No configured tools")
        value, manifest = await self.conversation_manifest(frame["checkpoint"], "before_tool")
        receipt = (
            await self.command(
                self.repo.begin_tool, frame, checkpoint=manifest, approval_required=False
            )
        )["tool"]
        if receipt["state"] == "completed":
            result = receipt["result"]["pi_result"]
        else:
            self.value, self.durable = value, manifest
            result = await self.bound_tools.execute(self.run, frame["name"], frame["input"])
            await self.command(
                self.repo.complete_tool,
                frame,
                result={"pi_result": result, "checkpoint": manifest},
                checkpoint=manifest,
            )
        await self.worker.send({"type": "reply", "id": frame["id"], "result": result})

    async def publish(self, grant):
        with database_stage("publication"):
            result = await self.publication.publish(self.run, self.value, grant, self.worker)
        await self.write("publication", result, publication=result)
        await self.finish(
            "succeeded" if result["status"] in {"committed", "no_changes"} else "conflict",
            "completed",
        )

    async def finish(self, state, code, *, retain_resource=False):
        await self.flush_text()
        # Cleanup happens before terminal ACK and is retried by takeover on a
        # crash. Provider deletion is safe only once recovery data is durable.
        if not self.finished:
            await self.write("heartbeat")
        completed_execution = self.finished and not retain_resource and self.worker is not None
        workspace_released = False
        if self.workspace and self.workspace.row and self.worker:
            with database_stage("sandbox_release"):
                if state == "succeeded" and not retain_resource:
                    await self.workspace.pause(self.run, self.worker)
                else:
                    await self.workspace.dispose(self.run, self.worker, retain=retain_resource)
            workspace_released = not retain_resource
        if not retain_resource and not workspace_released:
            executions = (
                [
                    {
                        "id": self.run["execution_id"],
                        "fence": self.run["fence"],
                        "resource": self.worker.resource,
                    }
                ]
                if completed_execution
                else await asyncio.to_thread(self.repo.executions, self.run["id"])
            )
            for execution in executions:
                if execution["fence"] > self.run["fence"]:
                    continue
                if execution["resource"]:
                    with database_stage("sandbox_cleanup"):
                        if (
                            self.worker
                            and execution["resource"]["session_id"] == self.worker.execution_id
                        ):
                            await self.worker.stop()
                        else:
                            await self.worker_lifecycle.cleanup(execution["resource"])
                if not completed_execution:
                    await asyncio.to_thread(self.repo.cleaned, execution["id"])
        if self.run["billing_run_id"] and not retain_resource:
            await self.billing.finish_session(self.run["billing_run_id"])
        if self.workspace and self.workspace.row is None and not retain_resource:
            await asyncio.to_thread(self.repo.workspace_recovered, self.run)
        snapshot = {**self.run["snapshot"], "code": code, "resource_retained": retain_resource}
        async with self.lock:
            self.run = await asyncio.to_thread(
                self.repo.complete_run,
                self.run,
                state=state,
                code=code,
                snapshot=snapshot,
                cleaned=bool(completed_execution or workspace_released),
            )
