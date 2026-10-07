"""Transactional Agent operations; SQL owns order, idempotency and fencing."""

from .queries import RunQueries


class RunRepository(RunQueries):
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
        return self.write(
            run,
            "terminal",
            {
                "state": state,
                "code": code,
                "resource_retained": snapshot.get("resource_retained", False),
            },
            state=state,
            snapshot=snapshot,
            **({"cleaned": True} if cleaned else {}),
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
