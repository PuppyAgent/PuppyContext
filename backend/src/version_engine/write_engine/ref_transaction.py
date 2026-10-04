"""Admitted native ref publication over durable objects and the PG authority.

This service cannot enroll/activate a repository. The caller selects checked
actor/lease, capacity and logical-billing capabilities; optional low-level
profiles are not canonical admission. Scope projection and full ref/file policy
remain separate gates; a bounded Scope credential cannot use this interface. No transport graph materialization
is imported by this write-engine service.
"""

from __future__ import annotations

import base64
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from src.platform.authorization.models import ProjectAction, ProjectGrant, RuntimeGrant
from src.platform.repository_target.models import ProjectRootTarget
from src.version_engine.infrastructure.supabase.billing_repository import RepositoryBilling
from src.version_engine.infrastructure.supabase.capacity_repository import RepositoryCapacity
from src.version_engine.infrastructure.supabase.file_policy_repository import RepositoryFilePolicy
from src.version_engine.infrastructure.supabase.ref_authority_repository import (
    RefAuthorityRepository,
)
from src.version_engine.storage.mutation_context import publication_storage
from src.version_engine.storage.object_store import StorageBackend
from src.version_engine.storage.publication import ClosureVerifier
from src.version_engine.write_engine.git_object_format import encode_object, object_id_bytes


def validate_ref_name(name: bytes) -> None:
    if name == b"HEAD":
        return
    if not isinstance(name, bytes) or not 6 <= len(name) <= 1024 or not name.startswith(b"refs/"):
        raise ValueError("invalid ref name")
    if (any(char <= 32 or char == 127 or char in b"~^:?*[\\" for char in name)
            or b".." in name or b"@{" in name or name.endswith(b".")):
        raise ValueError("invalid ref name")
    if any(not part or part.startswith(b".") or part.endswith(b".lock") for part in name.split(b"/")):
        raise ValueError("invalid ref name")


def publication_pin_id(project_id: str, actor: str, request_key: str) -> str:
    """Stable publication identity shared by preparation and final publication."""
    key = str(uuid.UUID(request_key))
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "puppyone:publication\0" + project_id + "\0" + actor + "\0" + key))


@dataclass(frozen=True)
class RefState:
    oid: str | None = None
    target: bytes | None = None

    def wire(self, object_format: str) -> dict:
        if self.oid is not None:
            width = object_id_bytes(object_format) * 2
            if (self.target is not None or len(self.oid) != width
                    or not set(self.oid) <= set("0123456789abcdef") or self.oid == "0" * width):
                raise ValueError("invalid ref object id")
            return {"kind": "oid", "oid": self.oid}
        if self.target is not None:
            validate_ref_name(self.target)
            if not self.target.startswith(b"refs/heads/"):
                raise ValueError("symbolic HEAD must target a branch")
            return {"kind": "symbolic", "target_b64": base64.b64encode(self.target).decode("ascii")}
        return {"kind": "absent"}


@dataclass(frozen=True)
class RefEdit:
    name: bytes
    expected: RefState
    new: RefState | None = None

    def wire(self, object_format: str) -> dict:
        validate_ref_name(self.name)
        if self.name != b"HEAD" and (self.expected.target is not None or (self.new and self.new.target is not None)):
            raise ValueError("only HEAD can be symbolic")
        if self.name == b"HEAD" and self.new == RefState():
            raise ValueError("HEAD cannot be deleted")
        value = {"name_b64": base64.b64encode(self.name).decode("ascii"),
                 "expected": self.expected.wire(object_format)}
        if self.new is not None:
            value["new"] = self.new.wire(object_format)
        return value


def admitted_actor(grant: ProjectGrant | RuntimeGrant, project_id: str, *, write: bool) -> str:
    if not isinstance(grant, (ProjectGrant, RuntimeGrant)) or grant.project_id != project_id:
        raise PermissionError("Project grant required")
    if isinstance(grant, ProjectGrant):
        if not grant.allows(ProjectAction.CONTENT_WRITE if write else ProjectAction.CONTENT_READ):
            raise PermissionError("Project action denied")
        return "user:" + grant.user_id
    if not isinstance(grant.target, ProjectRootTarget):
        raise PermissionError("Scope credentials cannot publish full repository refs")
    if write and not grant.can_write:
        raise PermissionError("read-only repository credential")
    return "runtime:" + grant.principal.principal_id


class RefTransactionService:
    def __init__(self, control: RefAuthorityRepository, backend: StorageBackend, *, project_id: str,
                 object_format: str = "sha1", max_objects: int = 1_000_000, max_bytes: int = 8 * 1024**3,
                 capacity: RepositoryCapacity | None = None, billing: RepositoryBilling | None = None,
                 policy: RepositoryFilePolicy | None = None):
        bound_project = getattr(backend, "publication_project_id", None)
        if bound_project is not None and bound_project != project_id:
            raise ValueError("publication backend belongs to another Project")
        if capacity is not None and capacity.control is not control:
            raise ValueError("capacity admission/control binding mismatch")
        if billing is not None and (billing.control is not control or capacity is None):
            raise ValueError("billing requires matching control and retained capacity admission")
        if policy is not None and (policy.control is not control or billing is None):
            raise ValueError("file policy requires matching control and billing admission")
        self.policy = policy
        self.billing = billing
        self.capacity = capacity
        self.control = control
        self.backend = backend
        self.project_id = project_id
        self.object_format = object_format
        self.verifier = ClosureVerifier(backend, object_format=object_format,
                                        max_objects=max_objects, max_bytes=max_bytes)

    def submit(
        self, grant: ProjectGrant | RuntimeGrant, *, request_key: str, generation: int,
        edits: Sequence[RefEdit], roots: Mapping[str, str], prepare: Callable[[], None], message: str = "",
    ) -> dict:
        # Recovering the original result is a read, not a new publication.
        # The guarded control rechecks current facts and the original digest.
        actor = admitted_actor(grant, self.project_id, write=False)
        request_key = str(uuid.UUID(request_key))
        if not 1 <= len(edits) <= 256 or generation < 1 or len(message.encode("utf-8")) > 8192:
            raise ValueError("invalid ref transaction")
        edits = tuple(edits)
        updates = [edit.wire(self.object_format) for edit in edits]
        if len({edit.name for edit in edits}) != len(edits):
            raise ValueError("duplicate ref")
        roots = dict(roots)
        desired = {edit.new.oid for edit in edits if edit.new is not None and edit.new.oid is not None}
        if set(roots) != desired or any(kind not in {"commit", "tree", "tag", "blob"} for kind in roots.values()):
            raise ValueError("publication roots must match direct targets")
        for edit in edits:
            if (edit.new is not None and edit.new.oid is not None
                    and (edit.name == b"HEAD" or edit.name.startswith(b"refs/heads/"))
                    and roots[edit.new.oid] != "commit"):
                raise ValueError("branch and HEAD targets must be commits")
        pin = publication_pin_id(self.project_id, actor, request_key) if roots else None
        args = (self.project_id, actor, request_key, generation, updates, pin, message)
        # Replay still calls apply: only the SQL request digest can prove that
        # this is the original request, including a previously rejected batch.
        if self.control.result(self.project_id, actor, request_key) is not None:
            if self.policy is not None:
                return self.policy.apply(*args)
            return self.billing.apply(*args) if self.billing is not None else self.control.apply(*args)
        admitted_actor(grant, self.project_id, write=True)
        snapshot = self.control.snapshot(self.project_id)
        if (not snapshot or snapshot["authority"] != "native" or snapshot["write_state"] != "active"
                or snapshot["object_format"] != self.object_format or snapshot["generation"] != generation):
            raise RuntimeError("repository authority unavailable or generation mismatch")
        if self.capacity is not None:
            self.capacity.check(self.project_id)
        billing_context = self.billing.check(self.project_id) if self.billing is not None else None
        policy_context = self.policy.check(self.project_id) if self.policy is not None else None
        manifest = None
        if pin is not None:
            admitted_pin = self.control.begin(self.project_id, actor, pin, generation, roots)
            next_renewal = time.monotonic() + 30
            def renew():
                nonlocal next_renewal
                if time.monotonic() >= next_renewal:
                    self.control.renew(self.project_id, actor, pin)
                    next_renewal = time.monotonic() + 30
            # A sealed pin is immutable. A retry after seal but before the ref
            # result must reuse its proof, not perform new location writes.
            if admitted_pin["state"] != "verified":
                # Retain the pin on failure/uncertain ACK. Storage mutations
                # revalidate its current state/epoch in PG, even if a copied
                # async context outlives this request or the pin's expiry.
                with publication_storage(self.project_id, actor, pin, require_capacity=self.capacity is not None):
                    prepare()
                    empty_oid, empty_loose = encode_object("tree", b"", object_format=self.object_format)
                    self.backend.put_durable(empty_oid, empty_loose)
                    manifest = self.verifier.verify(roots, progress=renew)
                    if self.capacity is not None:
                        self.capacity.seal(self.project_id, actor, pin, manifest)
                    else:
                        self.control.seal(self.project_id, actor, pin, manifest.digest, manifest.root_details())
        if pin is not None and manifest is None and self.billing is not None:
            # A sealed retry can contain not-yet-published roots. Verify its own
            # publication proof; never add those roots to a reader's snapshot.
            manifest = self.verifier.verify(roots, progress=renew)
        if self.billing is not None:
            usage = self.billing.measure(self, grant, edits, billing_context, manifest)
            if self.policy is not None:
                proof = self.policy.verify(self, grant, edits, roots, policy_context, manifest)
                result = self.policy.apply(*args, usage=usage, policy=proof)
            else:
                result = self.billing.apply(*args, usage=usage)
        else:
            result = self.control.apply(*args)
        if pin is not None:
            # The result is already durable. A cleanup outage retains objects,
            # and must not turn a committed publication into a false rejection.
            try:
                self.control.release(self.project_id, actor, pin)
            except Exception:
                from src.utils.logger import log_warning
                log_warning("native publication pin release deferred to expiry")
        return result
