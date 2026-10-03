"""Native repository RPC boundary; no legacy-upsert fallback or activation."""

from __future__ import annotations

from src.infra.supabase.client import SupabaseClient


class RefAuthorityRepository:
    def __init__(self, client=None):
        self.client = client if client is not None else SupabaseClient().client

    def call(self, function: str, **parameters):
        return self.client.rpc(function, parameters).execute().data

    def snapshot(self, project_id: str) -> dict | None:
        result = self.call("get_version_repository_snapshot", p_project_id=project_id)
        if result is not None and not isinstance(result, dict):
            raise RuntimeError("invalid repository snapshot response")
        return result

    def result(self, project_id: str, actor: str, request_key: str):
        return self.call("get_version_ref_transaction", p_project_id=project_id,
                         p_actor=actor, p_request_key=request_key)

    def begin(self, project_id: str, actor: str, pin: str, generation: int, roots: dict):
        return self.call("begin_version_object_publication", p_project_id=project_id,
                         p_actor=actor, p_pin_id=pin, p_generation=generation, p_roots=roots)

    def renew(self, project_id: str, actor: str, pin: str):
        return self.call("renew_version_object_publication", p_project_id=project_id,
                         p_actor=actor, p_pin_id=pin)

    def seal(self, project_id: str, actor: str, pin: str, digest: str, details: dict | None = None):
        return self.call("seal_version_object_publication", p_project_id=project_id,
                         p_actor=actor, p_pin_id=pin, p_manifest_sha256=digest, p_root_details=details)

    def release(self, project_id: str, actor: str, pin: str):
        return self.call("release_version_object_publication", p_project_id=project_id,
                         p_actor=actor, p_pin_id=pin)

    def apply(self, project_id: str, actor: str, request_key: str, generation: int,
              updates: list[dict], receipt: str | None, message: str):
        return self.call("apply_version_ref_transaction", p_project_id=project_id,
                         p_actor=actor, p_request_key=request_key, p_generation=generation,
                         p_updates=updates, p_receipt_id=receipt, p_message=message)

    def begin_read(self, project_id: str, actor: str, pin: str):
        return self.call("begin_version_repository_read", p_project_id=project_id,
                         p_actor=actor, p_pin_id=pin)

    def begin_gc(self, project_id: str, token: str):
        return self.call("begin_version_repository_gc", p_project_id=project_id, p_token=token)

    def finish_gc(self, project_id: str, token: str):
        return self.call("finish_version_repository_gc", p_project_id=project_id, p_token=token)
