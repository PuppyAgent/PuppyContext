BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='2min';

CREATE FUNCTION public._guard_bare_repository_transaction()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE role_name text;
BEGIN
    IF EXISTS(SELECT 1 FROM public.version_repository_billing
              WHERE project_id=NEW.project_id AND accounted_bytes IS NOT NULL)
       AND EXISTS(SELECT 1 FROM jsonb_array_elements(NEW.result->'refs') edit
                  WHERE edit->>'name_b64'='SEVBRA==' AND (edit->>'changed')::boolean) THEN
        -- Current role is checked inside the publication transaction while the
        -- admitted actor's membership rows are locked. A Runtime credential
        -- cannot change repository management metadata through crafted Git.
        IF NEW.actor NOT LIKE 'user:%' THEN
            RAISE EXCEPTION 'repository_management_denied' USING ERRCODE='42501';
        END IF;
        SELECT effective_role INTO role_name FROM public.resolve_project_role(
            NEW.project_id,substring(NEW.actor FROM 6)::uuid);
        IF role_name IS DISTINCT FROM 'admin' THEN
            RAISE EXCEPTION 'repository_management_denied' USING ERRCODE='42501';
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER bare_repository_management_guard BEFORE INSERT ON public.version_ref_transactions
    FOR EACH ROW EXECUTE FUNCTION public._guard_bare_repository_transaction();

CREATE FUNCTION public._guard_bare_repository_resources()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
    IF NOT EXISTS(SELECT 1 FROM public.version_repository_billing
                  WHERE project_id=NEW.project_id AND accounted_bytes IS NOT NULL) THEN RETURN NEW; END IF;
    IF TG_TABLE_NAME='version_repository_object_capacity' THEN
        IF NEW.body_bytes>67108864 THEN
            RAISE EXCEPTION 'repository_single_object_budget_exceeded' USING ERRCODE='54000';
        END IF;
    ELSE
        IF NOT EXISTS(SELECT 1 FROM public.version_repository_refs WHERE project_id=NEW.project_id AND name=NEW.name)
           AND (SELECT count(*) FROM public.version_repository_refs WHERE project_id=NEW.project_id)>=10000 THEN
            RAISE EXCEPTION 'repository_ref_budget_exceeded' USING ERRCODE='54000';
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER bare_repository_object_budget BEFORE INSERT ON public.version_repository_object_capacity
    FOR EACH ROW EXECUTE FUNCTION public._guard_bare_repository_resources();
CREATE TRIGGER bare_repository_ref_budget BEFORE INSERT ON public.version_repository_refs
    FOR EACH ROW EXECUTE FUNCTION public._guard_bare_repository_resources();
REVOKE ALL ON FUNCTION public._guard_bare_repository_transaction(),public._guard_bare_repository_resources()
    FROM PUBLIC,anon,authenticated,service_role;
COMMIT;
