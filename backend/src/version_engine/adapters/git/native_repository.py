"""Canonical Git transport over S3 objects and PG refs, without a local repo."""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi.responses import Response

from src.version_engine.read.repository_snapshot import repository_snapshot
from src.version_engine.storage.mutation_context import publication_context
from src.version_engine.write_engine.git_object_format import encode_object
from src.version_engine.write_engine.git_object_graph import object_edges
from src.version_engine.write_engine.ref_transaction import (
    RefState,
    RefTransactionService,
    admitted_actor,
)

from .execution import ObjectGitResponse, checkpoint
from .native_fetch import FetchRequest, fetch_chunks
from .native_wire import advertisement, ls_refs, receive_commands, upload_packets, version
from .object_pack import IncomingPack
from .object_reader import PublishedObjectReader
from .protocol import flush_pkt, pkt_line


class PublicationIndeterminateError(RuntimeError):
    """No definitive result was obtained; the client must rediscover refs."""


class NativeGitRepository:
    def __init__(self, service: RefTransactionService):
        self.service = service
        self.control = service.control
        self.project_id = service.project_id
        self.format = service.object_format

    def info_refs(self, grant, service: str, *, protocol=""):
        actor = admitted_actor(grant, self.project_id, write=False)
        if service not in {"git-upload-pack", "git-receive-pack"}:
            raise ValueError("unsupported Git service")
        snapshot = self.control.read_snapshot(self.project_id, actor)
        output = advertisement(snapshot, service, protocol, self.format)
        return Response(
            pkt_line(f"# service={service}\n".encode()) + flush_pkt() + output,
            media_type=f"application/x-{service}-advertisement",
            headers={"Cache-Control": "no-cache"},
        )

    def upload(self, grant, request_path: Path, *, protocol=""):
        actor = admitted_actor(grant, self.project_id, write=False)
        packets = upload_packets(request_path)
        protocol_version = version(protocol)
        commands = [
            value.strip()
            for value in packets
            if isinstance(value, bytes) and value.startswith(b"command=")
        ]
        if protocol_version == 2:
            if len(commands) != 1 or commands[0] not in {b"command=ls-refs", b"command=fetch"}:
                raise ValueError("unsupported protocol-v2 command")
            if commands[0] == b"command=ls-refs":
                snapshot = self.control.read_snapshot(self.project_id, actor)
                return Response(
                    ls_refs(snapshot, packets, self.format),
                    media_type="application/x-git-upload-pack-result",
                )
        elif commands:
            raise ValueError("protocol-v2 command without negotiation")
        request = FetchRequest(packets, self.format, protocol_version)
        context = repository_snapshot(
            self.control, self.service.backend, grant, project_id=self.project_id
        )
        read = context.__enter__()
        try:
            if read.object_format != self.format:
                raise ValueError("repository object format mismatch")
            reader = PublishedObjectReader(read, self.control)
            # Reject unreadable wants before HTTP headers are sent. No blob is
            # downloaded here, including a raw-OID partial-clone lazy fetch.
            reader.authorize([*request.wants, *request.shallow])
            return ObjectGitResponse(fetch_chunks(reader, request), context)
        except BaseException:
            context.__exit__(None, None, None)
            raise

    def commands(self, request_path):
        with request_path.open("rb") as handle:
            return receive_commands(handle, self.format)

    def receive(self, grant, request_path: Path):
        admitted_actor(grant, self.project_id, write=True)
        outcomes = {}
        with request_path.open("rb") as handle:
            edits, capabilities = receive_commands(handle, self.format)
            with repository_snapshot(
                self.control, self.service.backend, grant, project_id=self.project_id
            ) as snapshot:
                reader = PublishedObjectReader(snapshot, self.control)
                with IncomingPack(reader) as incoming:
                    try:
                        incoming.read(handle)
                    except (ValueError, KeyError, PermissionError) as exc:
                        return self.report(
                            edits, capabilities, {}, unpack="invalid pack: " + str(exc)
                        )
                    accepted, closures, roots = [], {}, {}
                    for edit in edits:
                        try:
                            if edit.name == b"HEAD":
                                raise ValueError("HEAD is changed through repository metadata")
                            if snapshot.refs.get(edit.name, RefState()) != edit.expected:
                                raise ValueError("stale ref")
                            closure = {}
                            if edit.new.oid is not None:
                                kind, _ = incoming.get(edit.new.oid)
                                if edit.name.startswith(b"refs/heads/") and kind != "commit":
                                    raise ValueError("branch target is not a commit")
                                roots[edit.name] = kind
                                stack = [(edit.new.oid, kind)]
                                seen = set()
                                while stack:
                                    checkpoint()
                                    oid, expected = stack.pop()
                                    if oid in seen:
                                        if oid in closure and closure[oid] != expected:
                                            raise ValueError("incoming edge type mismatch")
                                        continue
                                    seen.add(oid)
                                    if oid not in incoming.objects:
                                        reader.authorize([oid])
                                        if reader.allowed[oid] not in (None, expected):
                                            raise ValueError("published edge type mismatch")
                                        continue
                                    kind, body = incoming.get(oid)
                                    if kind != expected:
                                        raise ValueError("incoming edge type mismatch")
                                    closure[oid] = kind
                                    stack.extend(
                                        object_edges(kind, body, object_format=self.format)
                                    )
                            closures[edit.name] = closure
                            accepted.append(edit)
                        except (ValueError, KeyError, PermissionError) as exc:
                            outcomes[edit.name] = str(exc)
                    if "atomic" in capabilities and len(accepted) != len(edits):
                        accepted = []
                        outcomes.update({edit.name: "atomic receive rejected" for edit in edits})
                    batches = (
                        [accepted] if "atomic" in capabilities else [[edit] for edit in accepted]
                    )
                    uploaded = set()
                    for batch in batches:
                        if not batch:
                            continue
                        desired = {
                            edit.new.oid: roots[edit.name]
                            for edit in batch
                            if edit.new.oid is not None
                        }
                        pending = set().union(*(closures[edit.name] for edit in batch)) - uploaded

                        def prepare(pending=pending):
                            for oid in pending:
                                checkpoint()
                                snapshot.check_live()
                                context = publication_context.get()
                                if context is not None and context.progress is not None:
                                    context.progress()
                                kind, body = incoming.get(oid)
                                actual, loose = encode_object(kind, body, object_format=self.format)
                                if actual != oid:
                                    raise ValueError("incoming object hash mismatch")
                                self.service.backend.put_durable(oid, loose)
                                uploaded.add(oid)

                        request_key = str(uuid.uuid4())
                        try:
                            result = self.service.submit(
                                grant,
                                request_key=request_key,
                                generation=snapshot.generation,
                                edits=batch,
                                roots=desired,
                                prepare=prepare,
                                message="git push",
                            )
                        except Exception as exc:
                            actor = admitted_actor(grant, self.project_id, write=False)
                            try:
                                result = self.control.recover_result(
                                    self.project_id, actor, request_key
                                )
                            except Exception as lookup_error:
                                raise PublicationIndeterminateError(
                                    "Rediscover repository refs before retrying"
                                ) from lookup_error
                            if not result or result.get("status") not in {"committed", "rejected"}:
                                raise PublicationIndeterminateError(
                                    "Rediscover repository refs before retrying"
                                ) from exc
                        reason = None if result["status"] == "committed" else result["reason"]
                        outcomes.update({edit.name: reason for edit in batch})
        return self.report(edits, capabilities, outcomes)

    @staticmethod
    def report(edits, capabilities, outcomes, *, unpack="ok"):
        def safe(value):
            return (
                value.replace("\n", " ")
                .replace("\r", " ")
                .replace("\0", " ")[:1024]
                .encode("ascii", "replace")
            )

        report = pkt_line(b"unpack " + safe(unpack) + b"\n")
        for edit in edits:
            reason = outcomes.get(edit.name, "not published")
            line = (
                b"ok " + edit.name if reason is None else b"ng " + edit.name + b" " + safe(reason)
            )
            report += pkt_line(line + b"\n")
        report += flush_pkt()
        if "side-band-64k" in capabilities or "side-band" in capabilities:
            size = 65515 if "side-band-64k" in capabilities else 995
            report = (
                b"".join(
                    pkt_line(b"\x01" + report[i : i + size]) for i in range(0, len(report), size)
                )
                + flush_pkt()
            )
        return Response(
            report,
            media_type="application/x-git-receive-pack-result",
            headers={"Cache-Control": "no-cache"},
        )
