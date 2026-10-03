"""SQL provenance/epoch/ACL tests; synthetic locations are not S3 proof."""
from __future__ import annotations

import json
import uuid

import pytest

from tests.repository_hosting.harness.postgres import literal
from tests.repository_hosting.harness.ref_authority import A, Authority, B
from tests.repository_hosting.integration.test_publication_pins import begin, gc, rpc, seal

pytestmark = pytest.mark.hosting_live


def location(project, oid=A):
    return {"project_id": project, "object_id": oid,
            "pack_key": f"version/{project}/object-bundles/ff/{'f' * 64}.pob",
            "offset_bytes": 0, "size_bytes": 10}


def insert(row):
    return ("INSERT INTO public.version_object_locations(project_id,object_id,pack_key,offset_bytes,size_bytes) VALUES ("
            + ",".join(literal(row[k]) for k in ("project_id", "object_id", "pack_key", "offset_bytes", "size_bytes")) + ")")


def register(auth, pin, rows, *, actor="test:writer", check=True):
    return rpc(auth.pg, "register_version_object_locations", auth.project, actor, pin, rows, check=check)


def count(pg, project):
    return int(pg.value(f"SELECT count(*) FROM public.version_object_locations WHERE project_id={literal(project)}"))


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
def test_current_publication_pin_registers_project_bound_locations(pg_project, object_format):
    pg, project = pg_project
    oid = "a" * (40 if object_format == "sha1" else 64)
    auth = Authority(pg, project, object_format=object_format, roots={oid: "commit"})
    pin, _ = begin(auth, roots={oid: "commit"})
    row = location(project, oid)
    assert json.loads(register(auth, pin, [row]).stdout) == {"registered": 1}
    assert count(pg, project) == 1
    row["pack_key"] = f"chunked:version/{project}/object-bundles/chunked/{oid[:2]}/{oid}/manifest-{'e' * 64}.json"
    assert json.loads(register(auth, pin, [row]).stdout) == {"registered": 1}
    assert pg.value(f"SELECT pack_key FROM public.version_object_locations WHERE project_id={literal(project)}") == row["pack_key"]


@pytest.mark.parametrize("state", ["expired", "released", "sealed", "epoch", "generation", "read", "other-actor", "unknown-pin", "fenced"])
def test_invalid_pin_cannot_replace_an_existing_location(pg_project, state):
    pg, project = pg_project
    auth = Authority(pg, project)
    pin, _ = begin(auth)
    original = location(project)
    register(auth, pin, [original])
    actor = "test:writer"
    if state == "expired":
        pg.sql(f"UPDATE public.version_object_pins SET expires_at=clock_timestamp()-interval '1 second' WHERE id={literal(pin)}")
    elif state == "released":
        rpc(pg, "release_version_object_publication", project, actor, pin)
    elif state == "sealed":
        seal(auth, pin)
    elif state == "epoch":
        pg.sql(f"UPDATE public.version_repositories SET gc_epoch=gc_epoch+1 WHERE project_id={literal(project)}")
    elif state == "generation":
        pg.sql(f"UPDATE public.version_repositories SET generation=generation+1 WHERE project_id={literal(project)}")
    elif state == "read":
        pin = str(uuid.uuid4())
        rpc(pg, "begin_version_repository_read", project, actor, pin)
    elif state == "other-actor":
        actor = "test:other"
    elif state == "unknown-pin":
        pin = str(uuid.uuid4())
    else:
        pg.sql(f"UPDATE public.version_repositories SET write_state='fenced' WHERE project_id={literal(project)}")
    replacement = {**original, "offset_bytes": 1}
    result = register(auth, pin, [replacement], actor=actor, check=False)
    assert result.returncode
    assert "publication_pin_unavailable" in result.stderr or "repository_unavailable" in result.stderr
    assert pg.value(f"SELECT offset_bytes FROM public.version_object_locations WHERE project_id={literal(project)}") == "0"


@pytest.mark.parametrize("invalid", ["foreign-project", "foreign-key", "mutable-chunk", "bad-pack", "zero-size", "negative-offset", "other-format", "duplicate", "empty"])
def test_location_batch_validation_is_atomic(pg_project, invalid):
    pg, project = pg_project
    auth = Authority(pg, project)
    pin, _ = begin(auth)
    row = location(project, B)
    if invalid == "foreign-project":
        row["project_id"] = "another-project"
    elif invalid == "foreign-key":
        row["pack_key"] = row["pack_key"].replace(project, "another-project")
    elif invalid == "mutable-chunk":
        row["pack_key"] = f"chunked:version/{project}/object-bundles/chunked/{B[:2]}/{B}.json"
    elif invalid == "bad-pack":
        row["pack_key"] = row["pack_key"].replace("/ff/", "/aa/")
    elif invalid == "zero-size":
        row["size_bytes"] = 0
    elif invalid == "negative-offset":
        row["offset_bytes"] = -1
    elif invalid == "other-format":
        row["object_id"] = "b" * 64
    rows = [location(project), row]
    if invalid == "duplicate":
        rows.append(row)
    elif invalid == "empty":
        rows = []
    result = register(auth, pin, rows, check=False)
    assert result.returncode
    assert "invalid_object_locations" in result.stderr or "noncanonical_object_location" in result.stderr
    assert count(pg, project) == 0


@pytest.mark.parametrize("operation", ["insert", "update", "delete", "upsert"])
def test_direct_backend_native_location_mutation_is_fenced(pg_project, operation):
    pg, project = pg_project
    Authority(pg, project)
    pg.sql(insert(location(project)))  # Owner fixture/repair authority remains explicit.
    statement = {
        "insert": insert(location(project, B)),
        "update": f"UPDATE public.version_object_locations SET offset_bytes=1 WHERE project_id={literal(project)}",
        "delete": f"DELETE FROM public.version_object_locations WHERE project_id={literal(project)}",
        "upsert": insert(location(project)) + " ON CONFLICT(project_id,object_id) DO UPDATE SET offset_bytes=1",
    }[operation]
    result = pg.sql("SET ROLE service_role; SET app.native_storage_owner='postgres'; " + statement, check=False)
    assert result.returncode and "native_object_location_coordination_required" in result.stderr
    assert count(pg, project) == 1
    assert pg.value(f"SELECT offset_bytes FROM public.version_object_locations WHERE project_id={literal(project)}") == "0"


def test_legacy_location_dml_and_project_cascade_are_preserved(pg_project):
    pg, project = pg_project
    pg.sql("SET ROLE service_role; " + insert(location(project)))
    assert rpc(pg, "authorize_version_object_deletion", project, None).stdout.strip() == "t"
    pg.sql(f"SET ROLE service_role; UPDATE public.version_object_locations SET offset_bytes=1 WHERE project_id={literal(project)}")
    pg.sql(f"SET ROLE service_role; DELETE FROM public.version_object_locations WHERE project_id={literal(project)}")
    assert count(pg, project) == 0
    Authority(pg, project)
    pg.sql(insert(location(project)))
    # Project deletion remains the existing owner/control-plane operation;
    # this expansion must not grant service_role direct Project DELETE.
    denied = pg.sql(f"SET ROLE service_role; DELETE FROM public.projects WHERE id={literal(project)}", check=False)
    assert denied.returncode and "permission denied" in denied.stderr
    assert count(pg, project) == 1
    pg.sql(f"DELETE FROM public.projects WHERE id={literal(project)}")
    assert pg.value(f"SELECT count(*) FROM public.version_repositories WHERE project_id={literal(project)}") == "0"
    # The pre-existing location table has no Project FK; retain its explicit
    # cleanup contract, rather than inventing a cascading data deletion.
    assert count(pg, project) == 1
    pg.sql(f"SET ROLE service_role; DELETE FROM public.version_object_locations WHERE project_id={literal(project)}")
    assert count(pg, project) == 0


@pytest.mark.parametrize("direction", ["into-native", "out-of-native"])
def test_location_reparenting_cannot_escape_native_fence(pg_project, direction):
    pg, native = pg_project
    Authority(pg, native)
    legacy = pg.create_project()
    source, target = (legacy, native) if direction == "into-native" else (native, legacy)
    pg.sql(insert(location(source)))
    result = pg.sql(f"SET ROLE service_role; UPDATE public.version_object_locations SET project_id={literal(target)} "
                    f"WHERE project_id={literal(source)}", check=False)
    assert result.returncode and "native_object_location_coordination_required" in result.stderr
    assert count(pg, source) == 1 and count(pg, target) == 0


def test_only_current_gc_token_can_admit_deletion_or_remove_index(pg_project):
    pg, project = pg_project
    auth = Authority(pg, project)
    pg.sql(insert(location(project)))
    token = str(uuid.uuid4())
    assert rpc(pg, "authorize_version_object_deletion", project, token, check=False).returncode
    gc(auth, token)
    assert rpc(pg, "authorize_version_object_deletion", project, token).stdout.strip() == "t"
    # literal() serializes lists as JSON, so use explicit SQL arrays here.
    wrong = pg.sql(f"SET ROLE service_role; SELECT public.remove_version_object_locations("
                   f"{literal(project)},{literal(str(uuid.uuid4()))},ARRAY[{literal(A)}]);", check=False)
    assert wrong.returncode and "repository_gc_token_mismatch" in wrong.stderr
    assert count(pg, project) == 1
    assert json.loads(pg.value(f"SET ROLE service_role; SELECT public.remove_version_object_locations("
                              f"{literal(project)},{literal(token)},ARRAY[{literal(A)}]);")) == {"removed": 1}
    rpc(pg, "finish_version_repository_gc", project, token)
    result = rpc(pg, "authorize_version_object_deletion", project, token, check=False)
    assert result.returncode and "repository_gc_token_mismatch" in result.stderr


@pytest.mark.parametrize("role", ["anon", "authenticated"])
@pytest.mark.parametrize("function,arguments", [
    ("register_version_object_locations", "'project','actor',NULL,'[]'::jsonb"),
    ("remove_version_object_locations", "'project',NULL,ARRAY['oid']::text[]"),
    ("authorize_version_object_deletion", "'project',NULL"),
    ("_version_fence_object_locations", ""),
])
def test_client_roles_cannot_invoke_storage_coordination(pg_project, role, function, arguments):
    pg, _project = pg_project
    result = pg.sql(f"SET ROLE {role}; SELECT public.{function}({arguments});", check=False)
    assert result.returncode and "permission denied" in result.stderr
