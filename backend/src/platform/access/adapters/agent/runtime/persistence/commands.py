"""Transactional Agent operations; SQL owns order, idempotency and fencing."""

from uuid import UUID, uuid5

from .queries import RunQueries


class RunRepository(RunQueries):
    def workspace_acquire(self, run, binding):
        return self.rpc(
            "workspace_acquire",
            run=run["id"],
            execution=run["execution_id"],
            fence=run["fence"],
            binding=binding,
        )

    def workspace_transition(self, run, workspace, state, resource=None):
        return self.rpc(
            "workspace_transition",
            run=run["id"],
            execution=run["execution_id"],
            fence=run["fence"],
            generation=workspace["generation"],
            version=workspace["version"],
            state=state,
            resource=resource,
        )

    def workspace_due(self, limit=20):
        return self.rpc("workspace_due", limit=limit)

    def workspace_retired(self, workspace):
        return self.rpc(
            "workspace_retired",
            session=workspace["session_id"],
            generation=workspace["generation"],
            operation=workspace["operation_id"],
        )

    def workspace_recovered(self, run):
        return self.rpc(
            "workspace_recovered", run=run["id"], execution=run["execution_id"], fence=run["fence"]
        )

    def settle_model(self, run, *, checkpoint):
        return self.rpc(
            "settle_model",
            run=run["id"],
            execution=run["execution_id"],
            fence=run["fence"],
            checkpoint=checkpoint,
        )

    def start_execution(self, run, *, checkpoint, billing, resource):
        return self.rpc(
            "start_execution",
            run=run["id"],
            execution=run["execution_id"],
            fence=run["fence"],
            checkpoint=checkpoint,
            billing=billing,
            resource=resource,
        )

    def begin_model(self, run, *, request, checkpoint, limit):
        return self.rpc(
            "begin_model",
            run=run["id"],
            execution=run["execution_id"],
            fence=run["fence"],
            request=request,
            checkpoint=checkpoint,
            limit=limit,
        )

    def begin_tool(self, run, frame, *, checkpoint, mutation):
        return self.rpc(
            "begin_tool",
            run=run["id"],
            execution=run["execution_id"],
            fence=run["fence"],
            call=frame["call_id"],
            name=frame["name"],
            input=frame["input"],
            checkpoint=checkpoint,
            mutation=mutation,
        )

    def complete_tool(self, run, frame, *, result, checkpoint):
        return self.rpc(
            "complete_tool",
            run=run["id"],
            execution=run["execution_id"],
            fence=run["fence"],
            call=frame["call_id"],
            name=frame["name"],
            input=frame["input"],
            result=result,
            checkpoint=checkpoint,
        )

    def continue_after_rejection(self, run, frame):
        """Leave approval wait without changing its rejected receipt; ACK replay is inert."""
        batch = str(uuid5(UUID(run["execution_id"]), "declined-tool:" + frame["call_id"]))
        result = self.append_events_batch(
            run,
            [{"kind": "state", "payload": {"state": "running"}, "patch": {"state": "running"}}],
            batch_id=batch,
        )
        return {"run": result}

    def cleaned(self, execution_id):
        self.client.table("agent_run_executions").update({"cleaned": True}).eq(
            "id", execution_id
        ).execute()

    def submit_run(self, **values):
        return self.rpc("submit_context", **values)

    def claim_run(self, *, worker):
        value = self.rpc("claim_execution", worker=worker)
        if value is None:
            return None
        return {**value["run"], "_execution": value}

    def renew_execution(self, run):
        return self.rpc("renew", run=run["id"], execution=run["execution_id"], fence=run["fence"])

    def create_builtin(self, *, project, scope, user, config, revision):
        return self.rpc(
            "builtin_context",
            project=project,
            scope=scope,
            user=user,
            config=config,
            revision=revision,
        )

    def command_run(self, *, run, user, command, **values):
        return self.rpc("command_context", run=run, user=user, command=command, **values)

    def append_events_batch(self, run, events, *, batch_id):
        return self.rpc(
            "append_batch",
            run=run["id"],
            execution=run["execution_id"],
            fence=run["fence"],
            batch=batch_id,
            events=events,
        )

    def complete_run(self, run, *, state, code, snapshot, cleaned=False):
        return self.rpc(
            "finish",
            run=run["id"],
            execution=run["execution_id"],
            fence=run["fence"],
            state=state,
            code=code,
            snapshot=snapshot,
            publication=run["publication"],
            cleaned=cleaned,
        )

    def write(self, run, kind, payload=None, **patch):
        return self.rpc(
            "write",
            run=run["id"],
            execution=run["execution_id"],
            fence=run["fence"],
            kind=kind,
            payload=payload or {},
            patch=patch,
        )

    def tool(self, run, frame, state, result=None):
        return self.rpc(
            "tool",
            run=run["id"],
            execution=run["execution_id"],
            fence=run["fence"],
            call=frame["call_id"],
            name=frame["name"],
            input=frame["input"],
            state=state,
            result=result,
        )
