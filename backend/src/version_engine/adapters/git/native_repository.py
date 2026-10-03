"""Stock Git transport for explicitly native, full-Project repositories.

PG owns refs; S3 owns objects. Every local bare repository is request-owned and
throwaway. Nothing here activates a repository or broadens a Scope grant.
"""

from __future__ import annotations

import base64
import os
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from fastapi.responses import Response, StreamingResponse

from src.utils.logger import log_warning
from src.version_engine.adapters.git.protocol import flush_pkt, pkt_line, read_pkt_lines
from src.version_engine.write_engine.git_object_format import encode_object
from src.version_engine.write_engine.ref_transaction import (
    RefEdit,
    RefState,
    RefTransactionService,
    admitted_actor,
    validate_ref_name,
)


def accepted_receive_refs(output: bytes, capabilities: set[str]) -> set[bytes]:
    """Decode the actual report-status stream, never search arbitrary output."""
    if "side-band-64k" in capabilities or "side-band" in capabilities:
        payloads, _ = read_pkt_lines(output)
        if any(packet[:1] == b"\x03" for packet in payloads):
            return set()
        output = b"".join(packet[1:] for packet in payloads if packet[:1] == b"\x01")
    payloads, _ = read_pkt_lines(output)
    if not payloads or payloads[0] != b"unpack ok\n":
        return set()
    return {packet[3:-1] for packet in payloads[1:] if packet.startswith(b"ok ") and packet.endswith(b"\n")}


class NativeGitRepository:
    def __init__(self, service: RefTransactionService, *, timeout: int = 300, ref_storage: str | None = None):
        self.service = service
        self.control = service.control
        self.project_id = service.project_id
        self.format = service.object_format
        self.timeout = timeout
        # Linux files refs preserve byte/case distinctions. Default macOS
        # filesystems do not; use stock reftable there (tested Git 2.50.1).
        self.ref_storage = ref_storage or ("reftable" if sys.platform == "darwin" else "files")
        if self.ref_storage not in {"files", "reftable"}:
            raise ValueError("unsupported disposable Git ref backend")

    def environment(self, protocol=""):
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_NO_REPLACE_OBJECTS="1", GIT_TERMINAL_PROMPT="0", LC_ALL="C")
        versions = [part for part in protocol.split(":") if part.startswith("version=")]
        if versions:
            if len(versions) != 1 or versions[0] not in {"version=0", "version=1", "version=2"}:
                raise ValueError("unsupported Git protocol version")
            env["GIT_PROTOCOL"] = versions[0]
        return env

    def git(self, bare, *args, input=None, stdin=None, stdout=subprocess.PIPE, protocol="", check=True):
        command = ["git", "-c", "core.hooksPath=" + os.devnull,
                   "-c", "core.attributesFile=" + os.devnull,
                   "-c", "protocol.file.allow=never", "-c", "receive.fsckObjects=true",
                   "-c", "uploadpack.allowFilter=true", "-c", "uploadpack.allowAnySHA1InWant=true",
                   "-C", str(bare), *args]
        result = subprocess.run(command, input=input, stdin=stdin, stdout=stdout,
                                stderr=subprocess.PIPE, env=self.environment(protocol), timeout=self.timeout)
        if check and result.returncode:
            raise RuntimeError(result.stderr.decode("utf-8", "replace")[:2048])
        return result

    @contextmanager
    def bare(self, snapshot):
        with tempfile.TemporaryDirectory(prefix="puppyone-native-git-") as directory:
            path = Path(directory)
            self.git(path, "init", "--bare", "--initial-branch=main", "--object-format=" + self.format)
            packed = bytearray(b"# pack-refs with: peeled fully-peeled sorted\n")
            has_head = False
            for row in sorted(snapshot["refs"], key=lambda row: base64.b64decode(row["name_b64"])):
                name = base64.b64decode(row["name_b64"], validate=True)
                validate_ref_name(name)
                state = row["state"]
                if name == b"HEAD":
                    has_head = True
                    value = (b"ref: " + base64.b64decode(state["target_b64"], validate=True)
                             if state["kind"] == "symbolic" else state["oid"].encode("ascii"))
                    (path / "HEAD").write_bytes(value + b"\n")
                    continue
                if state["kind"] != "oid":
                    raise RuntimeError("unsupported persisted symbolic ref")
                if row.get("kind") is None:
                    raise RuntimeError("native ref has no verified root metadata")
                packed.extend(state["oid"].encode("ascii") + b" " + name + b"\n")
                if row.get("peeled_oid"):
                    packed.extend(b"^" + row["peeled_oid"].encode("ascii") + b"\n")
            if not has_head:
                raise RuntimeError("native repository has no authoritative HEAD")
            (path / "packed-refs").write_bytes(packed)
            yield path

    def info_refs(self, grant, service: str, *, protocol=""):
        admitted_actor(grant, self.project_id, write=False)
        if service not in {"git-upload-pack", "git-receive-pack"}:
            raise ValueError("unsupported Git service")
        snapshot = self.control.snapshot(self.project_id)
        if not snapshot or snapshot["authority"] != "native":
            raise RuntimeError("native repository unavailable")
        # Ref advertisement never traverses or reads canonical objects.
        with self.bare(snapshot) as bare:
            output = self.git(bare, service.removeprefix("git-"), "--stateless-rpc", "--advertise-refs", str(bare), protocol=protocol).stdout
        return Response(pkt_line(f"# service={service}\n".encode()) + flush_pkt() + output,
                        media_type=f"application/x-{service}-advertisement", headers={"Cache-Control": "no-cache"})

    @contextmanager
    def materialized(self, grant, *, historical=False, requested=()):
        actor = admitted_actor(grant, self.project_id, write=False)
        pin = str(uuid.uuid4())
        snapshot = self.control.begin_read(self.project_id, actor, pin)
        released = False
        def release():
            try:
                self.control.release(self.project_id, actor, pin)
            except Exception:
                log_warning("native read pin release deferred to expiry")
        try:
            with self.bare(snapshot) as bare:
                roots = {row["state"]["oid"]: row.get("kind") for row in snapshot["refs"]
                         if row["state"]["kind"] == "oid"}
                if historical:
                    roots.update(self.control.call("get_version_repository_published_roots", p_project_id=self.project_id))
                next_renewal = time.monotonic() + 30
                def renew():
                    nonlocal next_renewal
                    if time.monotonic() >= next_renewal:
                        self.control.renew(self.project_id, actor, pin)
                        next_renewal = time.monotonic() + 30
                def copy(oid, loose):
                    path = bare / "objects" / oid[:2] / oid[2:]
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(loose)
                if roots:
                    self.service.verifier.verify(roots, progress=renew, on_object=copy)
                if any(not (bare / "objects" / oid[:2] / oid[2:]).exists() for oid in requested):
                    # A GET advertisement or lazy-fetch base may precede a force
                    # update/delete. Serve retained *published* history, never
                    # arbitrary objects or uncommitted receipt roots.
                    roots.update(self.control.call("get_version_repository_published_roots", p_project_id=self.project_id))
                    if roots:
                        self.service.verifier.verify(roots, progress=renew, on_object=copy)
                # Every subsequent Git read is private; no alternates or S3 I/O.
                release()
                released = True
                yield bare, snapshot
        finally:
            if not released:
                release()

    def upload_request(self, request_path):
        command, requested = None, set()
        width = 40 if self.format == "sha1" else 64
        with request_path.open("rb") as handle:
            while header := handle.read(4):
                if len(header) != 4:
                    raise ValueError("truncated upload request")
                size = int(header, 16)
                if size in (0, 1, 2):
                    continue
                if not 4 <= size <= 65520:
                    raise ValueError("invalid upload pkt-line")
                payload = handle.read(size - 4)
                if len(payload) != size - 4:
                    raise ValueError("truncated upload pkt-line")
                if payload.startswith(b"command="):
                    command = payload.strip()
                if payload.startswith((b"want ", b"shallow ")):
                    oid = payload.split()[1].decode("ascii")
                    if len(oid) != width or not set(oid) <= set("0123456789abcdef"):
                        raise ValueError("invalid upload object hint")
                    requested.add(oid)
        return command, requested

    def upload(self, grant, request_path: Path, *, protocol=""):
        admitted_actor(grant, self.project_id, write=False)
        command, requested = self.upload_request(request_path)
        if command == b"command=ls-refs":
            # Protocol-v2 discovery is advertisement too, not a fetch. Packed
            # refs and their verified peel metadata require no object I/O.
            snapshot = self.control.snapshot(self.project_id)
            if not snapshot or snapshot["authority"] != "native":
                raise RuntimeError("native repository unavailable")
            with self.bare(snapshot) as bare, request_path.open("rb") as request:
                result = self.git(bare, "upload-pack", "--stateless-rpc", str(bare), stdin=request, protocol=protocol)
            return Response(result.stdout, media_type="application/x-git-upload-pack-result")
        # Ownership transfers to the response iterator, which closes on abort.
        output = tempfile.TemporaryFile()  # noqa: SIM115
        try:
            with self.materialized(grant, requested=requested) as (bare, _), request_path.open("rb") as request:
                self.git(bare, "upload-pack", "--stateless-rpc", str(bare), stdin=request, stdout=output, protocol=protocol)
            output.seek(0)
        except BaseException:
            output.close()
            raise
        def stream():
            try:
                while chunk := output.read(64 * 1024):
                    yield chunk
            finally:
                output.close()
        return StreamingResponse(stream(), media_type="application/x-git-upload-pack-result", headers={"Cache-Control": "no-cache"})

    def commands(self, request_path):
        edits, capabilities = [], set()
        zero = "0" * (40 if self.format == "sha1" else 64)
        with request_path.open("rb") as handle:
            while True:
                header = handle.read(4)
                if len(header) != 4:
                    raise ValueError("truncated receive request")
                size = int(header, 16)
                if size == 0:
                    break
                if not 4 <= size <= 65520:
                    raise ValueError("invalid receive pkt-line")
                payload = handle.read(size - 4)
                if len(payload) != size - 4:
                    raise ValueError("truncated receive command")
                if payload.startswith(b"shallow "):
                    continue
                command, sep, caps = payload.rstrip(b"\n").partition(b"\0")
                if sep:
                    if edits:
                        raise ValueError("capabilities after first command")
                    capabilities = set(caps.decode("ascii").split())
                old, new, name = command.split(b" ", 2)
                edit = RefEdit(name, RefState(oid=None if old.decode() == zero else old.decode()),
                               RefState(oid=None if new.decode() == zero else new.decode()))
                edit.wire(self.format)
                edits.append(edit)
                if len(edits) > 256:
                    raise ValueError("too many ref commands")
        if len({edit.name for edit in edits}) != len(edits):
            raise ValueError("duplicate ref command")
        return edits, capabilities

    def receive(self, grant, request_path: Path):
        admitted_actor(grant, self.project_id, write=True)
        edits, capabilities = self.commands(request_path)
        outcomes = {}
        with self.materialized(grant, historical=True) as (bare, snapshot):
            # Reftable makes the disposable receive namespace byte/case-safe on
            # macOS too. The canonical SQL still enforces files-prefix rules.
            if self.ref_storage == "reftable":
                self.git(bare, "refs", "migrate", "--ref-format=reftable")
            with request_path.open("rb") as request:
                official = self.git(bare, "receive-pack", "--stateless-rpc", str(bare), stdin=request, check=False)
            if not edits:
                return Response(official.stdout, media_type="application/x-git-receive-pack-result")
            reported = accepted_receive_refs(official.stdout, capabilities)
            accepted = []
            for edit in edits:
                current = self.git(bare, "show-ref", "--verify", "--hash", os.fsdecode(edit.name), check=False)
                matches = (current.returncode != 0 if edit.new.oid is None else current.stdout.strip().decode("ascii") == edit.new.oid)
                # Presence alone is insufficient when a no-change command was
                # rejected. Require an explicit stock report-status acceptance.
                if not matches or edit.name not in reported:
                    outcomes[edit.name] = "stock receive rejected update"
                else:
                    accepted.append(edit)
            if "atomic" in capabilities and len(accepted) != len(edits):
                accepted = []
                outcomes.update({edit.name: "atomic receive rejected" for edit in edits})
            batches = [accepted] if "atomic" in capabilities else [[edit] for edit in accepted]
            for batch in batches:
                if not batch:
                    continue
                roots = {}
                for edit in batch:
                    if edit.new.oid is not None:
                        roots[edit.new.oid] = self.git(bare, "cat-file", "-t", edit.new.oid).stdout.strip().decode("ascii")
                def prepare(roots=roots):
                    if not roots:
                        return
                    # Rev-list is transport-only; product writers never import
                    # this materializer. Gitlinks remain external dependencies.
                    ids = self.git(bare, "rev-list", "--objects", "--no-object-names", *roots).stdout.splitlines()
                    for raw_oid in ids:
                        oid = raw_oid.decode("ascii")
                        kind = self.git(bare, "cat-file", "-t", oid).stdout.strip().decode("ascii")
                        body = self.git(bare, "cat-file", kind, oid).stdout
                        actual, loose = encode_object(kind, body, object_format=self.format)
                        if actual != oid:
                            raise ValueError("quarantine object hash mismatch")
                        self.service.backend.put_durable(oid, loose)
                try:
                    result = self.service.submit(grant, request_key=str(uuid.uuid4()), generation=snapshot["generation"],
                                                 edits=batch, roots=roots, prepare=prepare, message="git push")
                    reason = None if result["status"] == "committed" else result["reason"]
                except Exception as exc:
                    reason = "publication failed: " + type(exc).__name__
                outcomes.update({edit.name: reason for edit in batch})
        report = pkt_line(b"unpack ok\n")
        for edit in edits:
            reason = outcomes.get(edit.name, "not published")
            line = b"ok " + edit.name if reason is None else b"ng " + edit.name + b" " + reason.encode("ascii", "replace")
            report += pkt_line(line + b"\n")
        report += flush_pkt()
        if "side-band-64k" in capabilities or "side-band" in capabilities:
            size = 65515 if "side-band-64k" in capabilities else 995
            report = b"".join(pkt_line(b"\x01" + report[i:i + size]) for i in range(0, len(report), size)) + flush_pkt()
        return Response(report, media_type="application/x-git-receive-pack-result", headers={"Cache-Control": "no-cache"})
