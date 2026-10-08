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
    def __init__(self, service: RefTransactionService, *, expected_generation=None):
        self.expected_generation = expected_generation
        self.service = service
        self.control = service.control
        self.project_id = service.project_id
        self.format = service.object_format

    def check_generation(self, generation):
        if self.expected_generation is not None and generation != self.expected_generation:
            raise PermissionError("Git repository generation changed")

    def info_refs(self, grant, service: str, *, protocol=""):
        actor = admitted_actor(grant, self.project_id, write=False)
        if service not in {"git-upload-pack", "git-receive-pack"}:
            raise ValueError("unsupported Git service")
        snapshot = self.control.read_snapshot(self.project_id, actor)
        self.check_generation(snapshot.get("generation"))
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
                self.check_generation(snapshot.get("generation"))
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
            self.check_generation(read.generation)
            read.backend = read.backend.pinned_reader(read)
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

    def receive(self, grant, request_path: Path, *, publication=None):
        admitted_actor(grant, self.project_id, write=True)
        outcomes = {}
        with request_path.open("rb") as handle:
            edits, capabilities = receive_commands(handle, self.format)
            if publication is not None:
                publication.validate(edits)
            with repository_snapshot(
                self.control, self.service.backend, grant, project_id=self.project_id
            ) as snapshot:
                self.check_generation(snapshot.generation)
                snapshot.backend = snapshot.backend.pinned_reader(snapshot)
                reader = PublishedObjectReader(snapshot, self.control)
                if publication is not None and snapshot.generation != publication.base.generation:
                    raise PermissionError("Run repository generation changed")
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
                            if publication is not None and publication.base.expected.oid:
                                ancestry, visited = [edit.new.oid], set()
                                ancestor = publication.base.expected.oid
                                while ancestry and ancestor not in visited:
                                    parent = ancestry.pop()
                                    if parent in visited:
                                        continue
                                    visited.add(parent)
                                    if parent == ancestor:
                                        break
                                    kind, body = incoming.get(parent)
                                    if kind != "commit":
                                        raise ValueError("Run ancestry is not a commit")
                                    ancestry.extend(
                                        oid
                                        for oid, kind in object_edges(
                                            kind, body, object_format=self.format
                                        )
                                        if kind == "commit"
                                    )
                                if ancestor not in visited:
                                    raise PermissionError(
                                        "Run candidate does not descend from its base"
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
                            # The canonical empty tree shares the incoming batch;
                            # it must not create a second physical lease/reservation.
                            empty_oid, empty_loose = encode_object("tree", b"", object_format=self.format)
                            batch, size = {empty_oid: empty_loose}, len(empty_loose)
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
                                if batch and (len(batch) >= 100 or size + len(loose) > 8 * 1024**2):
                                    self.service.backend.put_many_durable(batch)
                                    uploaded.update(batch)
                                    batch, size = {}, 0
                                batch[oid] = loose
                                size += len(loose)
                            if batch:
                                self.service.backend.put_many_durable(batch)
                                uploaded.update(batch)

                        request_key = (
                            publication.request_key
                            if publication is not None
                            else str(uuid.uuid4())
                        )
                        try:
                            result = self.service.submit(
                                grant,
                                request_key=request_key,
                                generation=snapshot.generation,
                                edits=publication.base.edits(publication.candidate)
                                if publication is not None
                                else batch,
                                roots=desired,
                                prepare=prepare,
                                message="git push",
                                read_snapshot=snapshot,
                                prepare_includes_empty_tree=True,
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
