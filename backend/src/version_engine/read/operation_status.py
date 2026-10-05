"""Read native operation provenance without opening objects or write admission."""

from __future__ import annotations

import re
import uuid

from src.version_engine.write_engine.ref_transaction import admitted_actor

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def operation_status(control, project_id: str, grant, request_key: str) -> dict | None:
    actor = admitted_actor(grant, project_id, write=False)
    key = str(uuid.UUID(request_key))
    lookup = getattr(control, "operation_status", None)
    if not callable(lookup):
        raise RuntimeError("native operation lookup unavailable")
    record = lookup(project_id, actor, key)
    if record is None:
        return None
    expected = {"project_id": project_id, "actor": actor, "request_key": key}
    if not isinstance(record, dict) or any(record.get(k) != v for k, v in expected.items()):
        raise RuntimeError("invalid native operation binding")
    keys = {*expected, "status", "input_sha256", "ref_request_sha256", "result", "product"}
    if (set(record) != keys or not isinstance(record["status"], str)
            or record["status"] not in {"pending", "committed", "rejected"}):
        raise RuntimeError("invalid native operation status")
    for name in ("input_sha256", "ref_request_sha256"):
        value = record[name]
        if value is not None and (not isinstance(value, str) or not _DIGEST.fullmatch(value)):
            raise RuntimeError("invalid native operation digest")
    result, product = record["result"], record["product"]
    if record["status"] == "pending":
        if record["input_sha256"] is None or any(record[k] is not None for k in ("result", "product", "ref_request_sha256")):
            raise RuntimeError("invalid pending native operation")
    elif (record["ref_request_sha256"] is None or not isinstance(result, dict)
          or result.get("project_id") != project_id or result.get("status") != record["status"]
          or type(result.get("generation")) is not int or result["generation"] < 1):
        raise RuntimeError("invalid native operation result binding")
    if product is not None and (not isinstance(product, dict) or record["status"] != "committed"
                                or record["input_sha256"] is None):
        raise RuntimeError("invalid native operation Product provenance")
    # Actor is an internal binding check, not a caller-selected query parameter.
    return {k: v for k, v in record.items() if k != "actor"}
