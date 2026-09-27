-- ISSUE-019 Phase 2: canonical physical names with zero-downtime compatibility.
BEGIN;

DROP VIEW IF EXISTS public.version_commits;
DROP VIEW IF EXISTS public.version_scope_state;
DROP VIEW IF EXISTS public.version_view_commits;
DROP VIEW IF EXISTS public.version_outbox;
DROP VIEW IF EXISTS public.version_conflicts;
DROP VIEW IF EXISTS public.version_object_locations;

ALTER TABLE public.mut_commits RENAME TO version_commits;
ALTER TABLE public.mut_scope_state RENAME TO version_scope_state;
ALTER TABLE public.mut_version_index RENAME TO version_view_commits;
ALTER TABLE public.mut_version_outbox RENAME TO version_outbox;
ALTER TABLE public.mut_conflicts RENAME TO version_conflicts;
ALTER TABLE public.mut_object_locations RENAME TO version_object_locations;

-- Direct column reads exist in the previous release, so columns use a
-- dual-write compatibility window instead of an unsafe one-shot rename.
ALTER TABLE public.projects ADD COLUMN IF NOT EXISTS version_root_hash TEXT;
UPDATE public.projects SET version_root_hash = COALESCE(mut_root_hash, '')
 WHERE version_root_hash IS NULL;
ALTER TABLE public.projects ALTER COLUMN version_root_hash SET DEFAULT '';
ALTER TABLE public.github_sync_log ADD COLUMN IF NOT EXISTS version_commit_id TEXT;
UPDATE public.github_sync_log SET version_commit_id = mut_commit_id
 WHERE version_commit_id IS NULL;

CREATE OR REPLACE FUNCTION public.sync_project_version_columns()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    NEW.version_root_hash := COALESCE(NEW.version_root_hash, NEW.mut_root_hash, '');
    NEW.mut_root_hash := NEW.version_root_hash;
  ELSIF NEW.version_root_hash IS DISTINCT FROM OLD.version_root_hash THEN
    NEW.mut_root_hash := NEW.version_root_hash;
  ELSIF NEW.mut_root_hash IS DISTINCT FROM OLD.mut_root_hash THEN
    NEW.version_root_hash := NEW.mut_root_hash;
  END IF;
  RETURN NEW;
END; $$;
DROP TRIGGER IF EXISTS projects_sync_version_columns ON public.projects;
CREATE TRIGGER projects_sync_version_columns
BEFORE INSERT OR UPDATE OF version_root_hash, mut_root_hash ON public.projects
FOR EACH ROW EXECUTE FUNCTION public.sync_project_version_columns();

CREATE OR REPLACE FUNCTION public.sync_github_version_columns()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    NEW.version_commit_id := COALESCE(NEW.version_commit_id, NEW.mut_commit_id);
    NEW.mut_commit_id := NEW.version_commit_id;
  ELSIF NEW.version_commit_id IS DISTINCT FROM OLD.version_commit_id THEN
    NEW.mut_commit_id := NEW.version_commit_id;
  ELSIF NEW.mut_commit_id IS DISTINCT FROM OLD.mut_commit_id THEN
    NEW.version_commit_id := NEW.mut_commit_id;
  END IF;
  RETURN NEW;
END; $$;
DROP TRIGGER IF EXISTS github_sync_log_sync_version_columns ON public.github_sync_log;
CREATE TRIGGER github_sync_log_sync_version_columns
BEFORE INSERT OR UPDATE OF version_commit_id, mut_commit_id ON public.github_sync_log
FOR EACH ROW EXECUTE FUNCTION public.sync_github_version_columns();

ALTER FUNCTION public.publish_mut_project_update(
 TEXT,TEXT,TEXT,TEXT,TEXT,TEXT,TEXT,JSONB,JSONB,TEXT,TEXT,JSONB,
 TEXT,TEXT,TEXT,TEXT,TEXT,TEXT,TEXT,TEXT,TEXT,TEXT
) RENAME TO publish_version_project_update;
ALTER FUNCTION public.get_mut_project_write_state(TEXT,TEXT)
 RENAME TO get_version_project_write_state;
ALTER FUNCTION public.claim_mut_version_outbox_batch(INT)
 RENAME TO claim_version_outbox_batch;
ALTER FUNCTION public.complete_mut_version_outbox(BIGINT)
 RENAME TO complete_version_outbox;
ALTER FUNCTION public.fail_mut_version_outbox(BIGINT,TEXT)
 RENAME TO fail_version_outbox;

-- Compatibility wrappers contain no domain logic and can overlap old/new pods.
CREATE FUNCTION public.publish_mut_project_update(
 p_project_id TEXT,p_old_root_hash TEXT,p_new_root_hash TEXT,p_head_commit_id TEXT,
 p_who TEXT,p_message TEXT,p_event_type TEXT,p_changes JSONB,p_conflicts JSONB,
 p_created_at TEXT,p_audit_agent_id TEXT,p_audit_detail JSONB,
 p_source_channel TEXT DEFAULT '',p_policy TEXT DEFAULT '',p_base_commit_id TEXT DEFAULT '',
 p_client_commit_id TEXT DEFAULT '',p_proposed_tree_id TEXT DEFAULT '',
 p_intent_type TEXT DEFAULT 'operation',p_scope_path TEXT DEFAULT '',
 p_scope_hash TEXT DEFAULT '',p_scope_head_commit_id TEXT DEFAULT '',
 p_expected_scope_head_commit_id TEXT DEFAULT NULL
) RETURNS TABLE(published BOOLEAN,txn_id BIGINT) LANGUAGE SQL AS $$
 SELECT * FROM public.publish_version_project_update(
  p_project_id,p_old_root_hash,p_new_root_hash,p_head_commit_id,p_who,p_message,
  p_event_type,p_changes,p_conflicts,p_created_at,p_audit_agent_id,p_audit_detail,
  p_source_channel,p_policy,p_base_commit_id,p_client_commit_id,p_proposed_tree_id,
  p_intent_type,p_scope_path,p_scope_hash,p_scope_head_commit_id,
  p_expected_scope_head_commit_id);
$$;
CREATE FUNCTION public.get_mut_project_write_state(p_project_id TEXT,p_user_id TEXT)
RETURNS TABLE(project_id TEXT,project_name TEXT,org_id TEXT,visibility TEXT,role TEXT,
 can_write BOOLEAN,root_hash TEXT,head_commit_id TEXT) LANGUAGE SQL AS $$
 SELECT * FROM public.get_version_project_write_state(p_project_id,p_user_id); $$;
CREATE FUNCTION public.claim_mut_version_outbox_batch(p_limit INT DEFAULT 50)
RETURNS TABLE(id BIGINT,project_id TEXT,commit_id TEXT,event_type TEXT,payload JSONB,
 attempts INT) LANGUAGE SQL AS $$
 SELECT * FROM public.claim_version_outbox_batch(p_limit); $$;
CREATE FUNCTION public.complete_mut_version_outbox(p_id BIGINT)
RETURNS BOOLEAN LANGUAGE SQL AS $$ SELECT public.complete_version_outbox(p_id); $$;
CREATE FUNCTION public.fail_mut_version_outbox(p_id BIGINT,p_error TEXT)
RETURNS BOOLEAN LANGUAGE SQL AS $$ SELECT public.fail_version_outbox(p_id,p_error); $$;

CREATE VIEW public.mut_commits WITH (security_invoker=true) AS SELECT * FROM public.version_commits;
CREATE VIEW public.mut_scope_state WITH (security_invoker=true) AS SELECT * FROM public.version_scope_state;
CREATE VIEW public.mut_version_index WITH (security_invoker=true) AS SELECT * FROM public.version_view_commits;
CREATE VIEW public.mut_version_outbox WITH (security_invoker=true) AS SELECT * FROM public.version_outbox;
CREATE VIEW public.mut_conflicts WITH (security_invoker=true) AS SELECT * FROM public.version_conflicts;
CREATE VIEW public.mut_object_locations WITH (security_invoker=true) AS SELECT * FROM public.version_object_locations;
REVOKE ALL ON public.mut_commits,public.mut_scope_state,public.mut_version_index,
 public.mut_version_outbox,public.mut_conflicts,public.mut_object_locations
 FROM PUBLIC,anon,authenticated;
GRANT SELECT,INSERT,UPDATE,DELETE ON public.mut_commits,public.mut_scope_state,
 public.mut_version_index,public.mut_version_outbox,public.mut_conflicts,
 public.mut_object_locations TO service_role;
REVOKE ALL ON FUNCTION public.get_version_project_write_state(TEXT,TEXT)
 FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.get_version_project_write_state(TEXT,TEXT) TO service_role;
REVOKE ALL ON FUNCTION public.publish_version_project_update(
 TEXT,TEXT,TEXT,TEXT,TEXT,TEXT,TEXT,JSONB,JSONB,TEXT,TEXT,JSONB,
 TEXT,TEXT,TEXT,TEXT,TEXT,TEXT,TEXT,TEXT,TEXT,TEXT),
 public.publish_mut_project_update(
 TEXT,TEXT,TEXT,TEXT,TEXT,TEXT,TEXT,JSONB,JSONB,TEXT,TEXT,JSONB,
 TEXT,TEXT,TEXT,TEXT,TEXT,TEXT,TEXT,TEXT,TEXT,TEXT),
 public.get_mut_project_write_state(TEXT,TEXT),
 public.claim_version_outbox_batch(INT),public.claim_mut_version_outbox_batch(INT),
 public.complete_version_outbox(BIGINT),public.complete_mut_version_outbox(BIGINT),
 public.fail_version_outbox(BIGINT,TEXT),public.fail_mut_version_outbox(BIGINT,TEXT)
 FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.publish_version_project_update(
 TEXT,TEXT,TEXT,TEXT,TEXT,TEXT,TEXT,JSONB,JSONB,TEXT,TEXT,JSONB,
 TEXT,TEXT,TEXT,TEXT,TEXT,TEXT,TEXT,TEXT,TEXT,TEXT),
 public.publish_mut_project_update(
 TEXT,TEXT,TEXT,TEXT,TEXT,TEXT,TEXT,JSONB,JSONB,TEXT,TEXT,JSONB,
 TEXT,TEXT,TEXT,TEXT,TEXT,TEXT,TEXT,TEXT,TEXT,TEXT),
 public.get_mut_project_write_state(TEXT,TEXT),
 public.claim_version_outbox_batch(INT),public.claim_mut_version_outbox_batch(INT),
 public.complete_version_outbox(BIGINT),public.complete_mut_version_outbox(BIGINT),
 public.fail_version_outbox(BIGINT,TEXT),public.fail_mut_version_outbox(BIGINT,TEXT)
 TO service_role;

NOTIFY pgrst,'reload schema';
COMMIT;
