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

    def read_snapshot(self, project_id: str, actor: str) -> dict | None:
        # Internal primitive profile. Authenticated adapters select the admitted
        # subclass, whose metadata reads revalidate the actor in SQL as well.
        return self.snapshot(project_id)

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


class AdmittedRefAuthorityRepository(RefAuthorityRepository):
    """Current-actor/lifecycle guarded writes; not a quota or routing switch.

    ``lease_provider(project_id)`` returns the caller's live ProjectWriteLease.
    Its fields convey provenance only: SQL revalidates them at execution time.
    The unguarded base class remains an internal primitive/testing boundary.
    """

    def __init__(self, client, *, lease_provider):
        super().__init__(client)
        self.lease_provider = lease_provider

    def read_snapshot(self, project_id: str, actor: str) -> dict:
        result = self.call("get_admitted_version_repository_snapshot", p_project_id=project_id, p_actor=actor)
        if not isinstance(result, dict):
            raise RuntimeError("invalid admitted repository snapshot response")
        return result

    def _lease(self, project_id):
        lease = self.lease_provider(project_id)
        if lease is None or not lease.is_active:
            # A committed/rejected result can still be replayed by a current
            # reader; SQL requires a lease only for a new mutation.
            return None, None
        if lease.project_id != project_id:
            raise ValueError("write lease Project binding mismatch")
        return lease.lease_id, lease.holder_id

    def begin(self, project_id: str, actor: str, pin: str, generation: int, roots: dict):
        lease, holder = self._lease(project_id)
        return self.call("begin_admitted_version_object_publication", p_project_id=project_id,
                         p_actor=actor, p_pin_id=pin, p_generation=generation, p_roots=roots,
                         p_lease_id=lease, p_holder_id=holder)

    def begin_read(self, project_id: str, actor: str, pin: str):
        return self.call("begin_admitted_version_repository_read", p_project_id=project_id,
                         p_actor=actor, p_pin_id=pin)

    def apply(self, project_id: str, actor: str, request_key: str, generation: int,
              updates: list[dict], receipt: str | None, message: str):
        lease, holder = self._lease(project_id)
        return self.call("apply_admitted_version_ref_transaction", p_project_id=project_id,
                         p_actor=actor, p_request_key=request_key, p_generation=generation,
                         p_updates=updates, p_receipt_id=receipt, p_message=message,
                         p_lease_id=lease, p_holder_id=holder)

    def renew(self, project_id: str, actor: str, pin: str):
        return self.call("renew_admitted_version_object_pin", p_project_id=project_id,
                         p_actor=actor, p_pin_id=pin)

    def check_write(self, project_id: str, actor: str):
        lease, holder = self._lease(project_id)
        return self.call("check_version_repository_write_admission", p_project_id=project_id,
                         p_actor=actor, p_lease_id=lease, p_holder_id=holder)

    def apply_billed(self, project_id: str, actor: str, request_key: str, generation: int,
                     updates: list[dict], receipt: str | None, message: str, *, usage=None):
        lease, holder = self._lease(project_id)
        return self.call("apply_billed_version_ref_transaction", p_project_id=project_id,
                         p_actor=actor, p_request_key=request_key, p_generation=generation,
                         p_updates=updates, p_receipt_id=receipt, p_message=message,
                         p_lease_id=lease, p_holder_id=holder, p_usage=usage)

    def apply_policy(self, project_id: str, actor: str, request_key: str, generation: int,
                     updates: list[dict], receipt: str | None, message: str, *, usage=None, policy=None):
        lease, holder = self._lease(project_id)
        return self.call("apply_policy_version_ref_transaction", p_project_id=project_id,
                         p_actor=actor, p_request_key=request_key, p_generation=generation,
                         p_updates=updates, p_receipt_id=receipt, p_message=message,
                         p_lease_id=lease, p_holder_id=holder, p_usage=usage, p_policy=policy)
