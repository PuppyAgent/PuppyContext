"""Capture one base, then publish through the existing canonical engine."""

import asyncio
import base64

from src.platform.access.adapters.agent.runtime.admission import clean_path, digest
from src.platform.access.adapters.agent.runtime.checkpoints import validate_files
from src.platform.access.adapters.agent.runtime.git_workspace import (
    capture_git,
    full_project,
    publish_git,
)
from src.platform.project.write_lease import ProjectWriteLease
from src.platform.repository_target.models import RepositoryPathProjection
from src.version_engine.adapters.batch.in_process_client import InProcessVersionClient
from src.version_engine.adapters.product.tree_patch import splice_batch


class PublicationRejected(ValueError):
    """A local preflight rejected changes before canonical publication began."""


def actor(run):
    return f"cloud-agent:{run['id']}:{run['execution_id']}:{run['fence']}"


def allowed(path, policy):
    prefix = policy["path_prefix"]
    return (not prefix or path == prefix or path.startswith(prefix + "/")) and not any(
        path == ex or path.startswith(ex + "/") for ex in policy["excludes"]
    )


class Publication:
    def __init__(self, container):
        self.container = container
        self.ops = container.product_operations()

    def capture(self, run, grant):
        policy, project = run["policy"], run["project_id"]
        files = {}
        workspace = {}
        with self.ops.open_read(project, grant) as reader:
            native = reader.get_read_revision(project)
            if native:
                full_project(run)
                workspace["git"] = capture_git(reader)
                workspace["modes"] = {}
            before = reader.get_head_commit_id(project)
            if policy["materialize"]:
                prefix = policy["path_prefix"]
                entry = reader.stat(project, prefix) if prefix else None
                if entry and entry.type != "folder":
                    entries = [entry]
                    mount = prefix.rpartition("/")[0]
                else:
                    entries = reader.list_tree(project, prefix, max_depth=-1)
                    mount = prefix
                for item in entries:
                    if item.type == "folder" or not allowed(item.path, policy):
                        continue
                    if getattr(item, "git_mode", None) in {"120000", "160000"}:
                        raise ValueError(
                            "Agent workspace cannot materialize symbolic links or submodules"
                        )
                    name = item.path[len(mount) + 1 :] if mount else item.path
                    files[name] = base64.b64encode(reader.read_file(project, item.path)).decode()
                    if native:
                        workspace["modes"][name] = getattr(item, "git_mode", None) or "100644"
                    validate_files(files)
            else:
                mount = policy["path_prefix"]
            if reader.get_head_commit_id(project) != before:
                raise RuntimeError("Repository changed during workspace capture; retry submission")
        return {
            **workspace,
            "version": 1,
            "pi_version": "0.85.1",
            "files": files,
            "base_files": dict(files),
            "base": native or {"repository_profile": "legacy", "head": before},
            "mount": mount,
            "entries": None,
            "leaf_id": None,
            "reason": "prepared",
        }

    async def publish(self, run, checkpoint, grant):
        if "git" in checkpoint:
            service = await asyncio.to_thread(
                self.container.repo_manager.get_native_service, run["project_id"]
            )
            if service is None:
                raise PublicationRejected("Native Agent repository is unavailable")
            lease = ProjectWriteLease(run["project_id"], "agent.git.publish", reuse_active=False)
            lease.holder_id = actor(run)
            async with lease:
                task = asyncio.create_task(
                    asyncio.to_thread(publish_git, service, run, checkpoint, grant)
                )
                try:
                    return await asyncio.shield(task)
                except asyncio.CancelledError:
                    try:
                        await task
                    finally:
                        raise
        try:
            files = validate_files(checkpoint["files"])
            original = validate_files(checkpoint["base_files"])
        except (ValueError, TypeError) as exc:
            raise PublicationRejected("Invalid workspace publication") from exc
        modified = {
            name: base64.b64decode(value, validate=True)
            for name, value in files.items()
            if original.get(name) != value
        }
        deleted = sorted(set(original) - set(files))
        if not modified and not deleted:
            return {"status": "no_changes"}
        if run["policy"]["readonly"] or not run["policy"]["materialize"]:
            raise PublicationRejected("Agent has no publication capability")
        mount = checkpoint["mount"]

        def full(name):
            return f"{mount}/{name}" if mount else name

        for name in [*modified, *deleted]:
            if clean_path(name) != name or not allowed(full(name), run["policy"]):
                raise PublicationRejected("Agent modification exceeds configured view")
        base = checkpoint["base"]
        if base["repository_profile"] == "native":
            operations = [("put", full(name), value) for name, value in modified.items()]
            operations += [("rm", full(name)) for name in deleted]

            def splice(store, root):
                return splice_batch(store, root, operations)

            def lease(project, operation):
                value = ProjectWriteLease(project, operation, reuse_active=False)
                value.holder_id = actor(run)
                return value

            result = await self.ops.apply_native_command(
                run["project_id"],
                grant,
                request_key=run["id"],
                base=base,
                input_sha256=digest(
                    {"version": 1, "files": files, "deleted": deleted, "mount": mount}
                ),
                splice=splice,
                message="Agent run " + run["id"],
                write_lease_factory=lease,
            )
            return result
        projection = RepositoryPathProjection(
            path_prefix=mount, excludes=tuple(run["policy"]["excludes"]), mode="rw"
        )
        client = InProcessVersionClient(
            self.container.repo_manager,
            run["project_id"],
            projection,
            actor=actor(run),
            source_channel="agent",
        )
        client.restore_base(
            head_commit_id=base["head"], files={p: base64.b64decode(v) for p, v in original.items()}
        )
        async with ProjectWriteLease(run["project_id"], "agent.publish"):
            result = await asyncio.to_thread(
                client.push,
                modified,
                deleted,
                "Agent run " + run["id"],
                policy_override="manual_review",
            )
        status = result.get("status")
        return {**result, "status": "committed" if status in {"ok", "committed"} else "conflict"}
