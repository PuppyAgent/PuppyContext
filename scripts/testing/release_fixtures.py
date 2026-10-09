"""Synthetic populated release data; called only by the owned Docker rehearsal."""

import json


def seed(db, project_id):
    if db.scalar("SELECT to_regclass('public.connections') IS NULL") == "t":
        return False
    db.scalar(
        """
INSERT INTO public.connections(id,org_id,project_id,provider,name,direction,config,target_path,created_by,status)
SELECT 'release-'||v.kind,p.org_id,p.id,v.provider,'Release fixture '||v.kind,'inbound',v.config::jsonb,
       '',p.created_by,'paused'
FROM public.projects p CROSS JOIN (VALUES
 ('binding','url','{"source":{"resource_url":"https://example.invalid"},"sentinel":"keep-binding"}'),
 ('source','database','{"db_provider":"supabase","db_config":{"ciphertext":"synthetic-only"},"sentinel":"keep-source"}'),
 ('dual','url','{"source":{"resource_url":"https://example.invalid"},"db_provider":"supabase","db_config":{"ciphertext":"synthetic-dual"},"sentinel":"keep-dual"}')
) AS v(kind,provider,config) WHERE p.id=:'project';
INSERT INTO public.sync_runs(id,connection_id,project_id,triggered_by,direction,status,result)
VALUES ('release-history','release-dual',:'project','manual','inbound','failed','{"sentinel":"keep-history"}');
UPDATE public.connections SET last_sync_run_id='release-history' WHERE id='release-dual';
""",
        variables={"project": project_id},
    )
    return True


def decisions(inventory):
    policy = {
        "release-binding": ("synchronize", None),
        "release-source": ("import", "release-import-source"),
        "release-dual": ("both", "release-import-dual"),
    }
    if {row["legacy_id"] for row in inventory} != set(policy):
        raise AssertionError("Unexpected source outside the owned release fixture")
    for row in inventory:
        disposition, source = policy[row["legacy_id"]]
        row.update(
            disposition=disposition,
            import_database_source_id=source,
            approved_by="owned-release-fixture",
            evidence_ref="scripts/testing/release_fixtures.py",
        )
    return {"format_version": 1, "rows": inventory}


def verify(db):
    actual = json.loads(
        db.scalar("""
SELECT jsonb_build_object(
 'bindings',(SELECT jsonb_object_agg(id,config->>'sentinel') FROM public.synchronize_bindings WHERE id LIKE 'release-%'),
 'sources',(SELECT jsonb_object_agg(id,config->>'sentinel') FROM public.import_database_sources WHERE id LIKE 'release-%'),
 'history',(SELECT result->>'sentinel' FROM public.synchronize_runs WHERE id='release-history'));
""")
    )
    assert actual == {
        "bindings": {"release-binding": "keep-binding", "release-dual": "keep-dual"},
        "sources": {
            "release-import-source": "keep-source",
            "release-import-dual": "keep-dual",
        },
        "history": "keep-history",
    }, actual
