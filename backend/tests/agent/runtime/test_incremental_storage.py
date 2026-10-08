"""Actual PG/PostgREST/S3 publication work and invalidated-boundary failures."""

from collections import Counter

import pytest

from src.infra.supabase.instrumentation import DatabaseTrace, database_trace
from tests.agent.runtime.test_supervisor import native_write
from tests.agent.runtime.test_supervisor import prepared as prepared_fixture

prepared = prepared_fixture
pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_small_write_does_not_reread_history_in_real_storage(prepared, services):
    case = prepared
    _, storage, _ = services
    await native_write(case, "a.md", b"baseline")
    requests = []

    def record(**kwargs):
        requests.append(kwargs["event_name"])

    clients = {storage.client, storage.for_single_attempt_io().client}
    for client in clients:
        client.meta.events.register("before-send.s3", record)

    async def measured(content):
        requests.clear()
        trace = DatabaseTrace("incremental-write")
        with database_trace(trace):
            await native_write(case, "a.md", content)
        return trace.report(), Counter(requests)

    try:
        short, short_s3 = await measured(b"small-change-one")
        for index in range(20):
            await native_write(case, "a.md", f"intermediate-{index}".encode())
        long, long_s3 = await measured(b"small-change-two")
        assert long["failures"] == short["failures"] == 0
        assert long["attempts"] <= short["attempts"] + 2, (short, long)
        assert sum(long_s3.values()) <= sum(short_s3.values()) + 2, (short_s3, long_s3)
        assert not any("ListObjects" in key for key in long_s3)
        print(
            {
                "short_history_pg": short["attempts"],
                "long_history_pg": long["attempts"],
                "short_history_s3": dict(short_s3),
                "long_history_s3": dict(long_s3),
            }
        )
        counts = case.postgres.row(
            f"SELECT count(*) AS objects,count(*) FILTER(WHERE published AND valid) AS reusable FROM version_object_proofs WHERE project_id='{case.project}'"
        )
        assert counts["objects"] == counts["reusable"]
    finally:
        for client in clients:
            client.meta.events.unregister("before-send.s3", record)


@pytest.mark.asyncio
async def test_known_bad_dependency_invalidates_ancestors_and_blocks_push(prepared, services):
    case = prepared
    client, _, _ = services
    await native_write(case, "a.md", b"retained content")
    grant = case.admission.authorization.resolve_project_grant(case.project, case.user)
    with case.ops.open_read(case.project, grant) as reader:
        before = reader.get_head_commit_id(case.project)
    blob = case.postgres.row(
        f"SELECT object_id FROM version_object_proofs WHERE project_id='{case.project}' AND kind='blob'"
    )["object_id"]
    response = (
        client.rpc(
            "invalidate_version_object_proofs", {"p_project_id": case.project, "p_oids": [blob]}
        )
        .execute()
        .data
    )
    assert response >= 3  # blob, its tree, its commit
    with pytest.raises(Exception, match="invalidated"):
        await native_write(case, "a.md", b"new content")
    with case.ops.open_read(case.project, grant) as reader:
        assert reader.get_head_commit_id(case.project) == before


@pytest.mark.asyncio
async def test_damage_between_proof_registration_and_seal_cannot_publish(
    prepared, services, monkeypatch
):
    from src.version_engine.infrastructure.supabase.object_proofs import ObjectProofRepository

    case = prepared
    client, _, _ = services
    await native_write(case, "a.md", b"base")
    original = ObjectProofRepository.persist

    def damage(repository, manifest):
        original(repository, manifest)
        if repository.project_id == case.project:
            client.rpc(
                "invalidate_version_object_proofs",
                {
                    "p_project_id": case.project,
                    "p_oids": list(manifest.roots),
                },
            ).execute()

    monkeypatch.setattr(ObjectProofRepository, "persist", damage)
    with pytest.raises(Exception, match="invalidated"):
        await native_write(case, "a.md", b"candidate")
    assert (
        case.postgres.sql(
            f"SELECT count(*) FROM version_ref_transactions WHERE project_id='{case.project}' AND result->>'status'='committed'"
        )
        == "1"
    )


def test_object_proofs_are_backend_only(postgres):
    for role in ("anon", "authenticated", "service_role"):
        assert (
            postgres.sql(f"SELECT has_table_privilege('{role}','version_object_proofs','UPDATE')")
            == "f"
        )
    assert (
        postgres.sql(
            "SELECT has_function_privilege('authenticated','get_version_object_proofs(text,text,uuid,jsonb,text)','EXECUTE')"
        )
        == "f"
    )
