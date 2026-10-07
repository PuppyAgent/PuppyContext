-- Project-bounded operator cutover. Historical inventories are not release gates.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='2min';
CREATE FUNCTION public.activate_prepared_native_repository_projects(
    p_org text,p_projects text[],p_reconciliation uuid)
RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE job public.version_repository_migrations%ROWTYPE; n bigint:=0;
    bytes bigint; objects bigint; entitlement jsonb; item record;
BEGIN
    IF p_projects IS NULL OR cardinality(p_projects) NOT BETWEEN 1 AND 200
       OR cardinality(p_projects)<>(SELECT count(DISTINCT v) FROM unnest(p_projects) v) THEN
        RAISE EXCEPTION 'migration_invalid_project_selection';
    END IF;
    PERFORM public._version_lock_storage_organization(p_org);
    PERFORM 1 FROM public.projects WHERE org_id=p_org ORDER BY id FOR UPDATE;
    IF (SELECT count(*) FROM public.projects p JOIN public.version_repository_migrations m ON m.project_id=p.id
        JOIN public.version_repositories r ON r.project_id=p.id
        WHERE p.id=ANY(p_projects) AND p.org_id=p_org AND p.lifecycle_status='ready'
          AND ((m.state='prepared' AND r.authority='shadow' AND r.write_state='fenced')
            OR (m.state='activated' AND r.authority='native')) )<>cardinality(p_projects) THEN
        RAISE EXCEPTION 'migration_selected_project_unavailable';
    END IF;
    IF NOT EXISTS(SELECT FROM public.version_repository_migrations
        WHERE project_id=ANY(p_projects) AND state='prepared') THEN RETURN 0; END IF;
    -- A one-time current-tree usage capture includes siblings without importing
    -- their historical graphs. Its existing CAS checks the entire current
    -- organization snapshot, so a partial/stale billing baseline cannot pass.
    IF NOT EXISTS(SELECT FROM public.version_storage_reconciliations
        WHERE id=p_reconciliation AND org_id=p_org AND result IS NULL)
       OR EXISTS(SELECT FROM public.version_repository_migrations m
          LEFT JOIN public.version_storage_reconciliation_projects s
            ON s.reconciliation_id=p_reconciliation AND s.project_id=m.project_id
          WHERE m.project_id=ANY(p_projects) AND m.state='prepared'
            AND s.logical_bytes IS DISTINCT FROM m.logical_bytes) THEN
        RAISE EXCEPTION 'migration_current_usage_required';
    END IF;
    PERFORM public.finish_version_storage_reconciliation(p_org,p_reconciliation);
    entitlement:=public._version_file_policy_limit(p_org);
    FOR job IN SELECT m.* FROM public.version_repository_migrations m JOIN public.projects p ON p.id=m.project_id
        WHERE p.org_id=p_org AND m.project_id=ANY(p_projects) AND m.state='prepared' ORDER BY m.project_id FOR UPDATE OF m LOOP
        IF job.source IS DISTINCT FROM public._native_migration_source(job.project_id) THEN RAISE EXCEPTION 'migration_source_changed'; END IF;
        IF EXISTS(SELECT 1 FROM public.project_write_leases WHERE project_id=job.project_id AND expires_at>clock_timestamp()) THEN
            RAISE EXCEPTION 'migration_writers_not_drained'; END IF;
        IF EXISTS(SELECT 1 FROM public.version_repository_refs WHERE project_id=job.project_id) THEN RAISE EXCEPTION 'migration_refs_not_empty'; END IF;
        SELECT count(*),sum(body_bytes) INTO objects,bytes FROM (SELECT DISTINCT object_id,body_bytes
            FROM public.version_repository_migration_objects WHERE project_id=job.project_id) o;
        INSERT INTO public.version_organization_capacity(org_id,initialized,max_body_bytes,max_objects)
            VALUES(p_org,true,34359738368,4000000) ON CONFLICT(org_id) DO NOTHING;
        UPDATE public.version_organization_capacity SET used_body_bytes=used_body_bytes+bytes,used_objects=used_objects+objects
            WHERE org_id=p_org AND initialized
              AND (max_body_bytes IS NULL OR used_body_bytes+bytes<=max_body_bytes)
              AND (max_objects IS NULL OR used_objects+objects<=max_objects);
        IF NOT FOUND THEN RAISE EXCEPTION 'migration_organization_capacity_exceeded'; END IF;
        INSERT INTO public.version_repository_capacity(project_id,org_id,initialized,max_body_bytes,max_objects,used_body_bytes,used_objects)
            VALUES(job.project_id,p_org,true,268435456,100000,bytes,objects);
        INSERT INTO public.version_repository_object_capacity(project_id,object_id,object_kind,body_bytes,admitted_pin_id)
            SELECT DISTINCT project_id,object_id,object_kind,body_bytes,job.id FROM public.version_repository_migration_objects WHERE project_id=job.project_id;
        INSERT INTO public.version_repository_billing(project_id,org_id,initialized,accounted_bytes)
            VALUES(job.project_id,p_org,true,job.logical_bytes);
        INSERT INTO public.version_repository_file_policies(project_id,initialized) VALUES(job.project_id,true);
        FOR item IN SELECT * FROM jsonb_each_text(job.refs) LOOP
            INSERT INTO public.version_repository_refs(project_id,name,object_format,target_oid)
                VALUES(job.project_id,convert_to(item.key,'UTF8'),'sha1',item.value);
        END LOOP;
        INSERT INTO public.version_repository_refs(project_id,name,object_format,symbolic_target)
            VALUES(job.project_id,convert_to('HEAD','UTF8'),'sha1',convert_to('refs/heads/main','UTF8'));
        UPDATE public.version_repositories SET authority='native',write_state='active',generation=generation+1
            WHERE project_id=job.project_id AND authority='shadow' AND write_state='fenced' AND gc_token IS NULL;
        IF NOT FOUND THEN RAISE EXCEPTION 'migration_authority_changed'; END IF;
        UPDATE public.version_repository_migrations SET state='activated',activated_at=clock_timestamp() WHERE project_id=job.project_id;
        INSERT INTO public.audit_logs(action,operator_type,operator_id,project_id,status,metadata)
            VALUES('repository_migrated','system','native-project-rollout',job.project_id,'committed',
                jsonb_build_object('migration_id',job.id,'manifest_sha256',job.manifest_sha256));
        n:=n+1;
    END LOOP;
    RETURN n;
END $$;
REVOKE ALL ON FUNCTION public.activate_prepared_native_repository_projects(text,text[],uuid)
    FROM PUBLIC,anon,authenticated,service_role;
COMMIT;
