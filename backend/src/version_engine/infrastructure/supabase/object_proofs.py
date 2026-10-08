"""Publication-scoped immutable facts; bounded RPCs, never a global inventory."""

from __future__ import annotations

import json

from src.version_engine.storage.publication import ClosureVerificationError, VerifiedObject


class ObjectProofRepository:
    def __init__(self, control, project_id, actor, pin, object_format, *, purpose="publication"):
        self.control, self.project_id, self.actor, self.pin = control, project_id, actor, pin
        self.object_format = object_format
        self.purpose = purpose
        self.cache = {}
        self.published = set()

    def prefetch(self, oids):
        missing = list(dict.fromkeys(oid for oid in oids if oid not in self.cache))
        for offset in range(0, len(missing), 200):
            batch = missing[offset : offset + 200]
            rows = self.control.call(
                "get_version_object_proofs",
                p_project_id=self.project_id,
                p_actor=self.actor,
                p_pin_id=self.pin,
                p_oids=batch,
                p_purpose=self.purpose,
            )
            if not isinstance(rows, dict) or set(rows) - set(batch):
                raise RuntimeError("invalid object proof response")
            for oid in batch:
                row = rows.get(oid)
                record = None
                if row is not None:
                    if (
                        row["project_id"] != self.project_id
                        or row["object_id"] != oid
                        or row["object_format"] != self.object_format
                        or not row["valid"]
                    ):
                        raise ClosureVerificationError("invalidated or mismatched object proof")
                    if row["published"]:
                        self.published.add(oid)
                    record = VerifiedObject(
                        row["kind"],
                        row["body_bytes"],
                        row["body_sha256"],
                        tuple(tuple(edge) for edge in row["edges"]),
                        row["logical_bytes"],
                        row["max_blob_bytes"],
                    )
                self.cache[oid] = record

    def get(self, oid):
        self.prefetch([oid])
        return self.cache[oid]

    def persist(self, manifest):
        batch, size = [], 0

        def flush():
            if not batch:
                return
            count = self.control.call(
                "register_version_object_proofs",
                p_project_id=self.project_id,
                p_actor=self.actor,
                p_pin_id=self.pin,
                p_rows=batch,
            )
            if count != len(batch):
                raise RuntimeError("incomplete durable object proof acknowledgement")
            for row in batch:
                self.cache[row["object_id"]] = manifest.objects[row["object_id"]]

        # Verification supplies dependency order. Each old boundary is already
        # published or belongs to this exact admitted retry; it is not rewritten.
        for oid in manifest.new_objects:
            record = manifest.objects[oid]
            row = {
                "object_id": oid,
                "kind": record.kind,
                "body_bytes": record.size,
                "body_sha256": record.body_sha256,
                "edges": list(record.edges),
                "logical_bytes": record.logical_bytes,
                "max_blob_bytes": record.max_blob_bytes,
            }
            encoded_size = len(json.dumps(row).encode())
            if encoded_size > 2 * 1024**2:
                raise ClosureVerificationError("object proof metadata budget exceeded")
            if batch and (len(batch) == 200 or size + encoded_size > 2 * 1024**2):
                flush()
                batch, size = [], 0
            batch.append(row)
            size += encoded_size
        flush()
