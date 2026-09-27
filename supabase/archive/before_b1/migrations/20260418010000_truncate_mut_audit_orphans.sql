-- ============================================================
-- Follow-up to 20260418000000_mut_commit_id_identity.sql
--
-- WHY
--   The commit-id identity migration TRUNCATEd mut_commits /
--   mut_scope_state but only ALTERed audit_logs (dropped the two
--   integer version columns). That leaves every pre-migration
--   push/clone/pull/rollback/merge_conflict audit row visible in
--   the System Monitor, even though the mut_commits row it refers
--   to no longer exists. Net effect: the UI shows 46 "push" events
--   but "Commit History — 0 commits". The two pages disagree.
--
-- WHAT THIS DOES
--   DELETE audit_logs rows whose ``action`` belongs to the MUT
--   protocol event set. These are the nine event types emitted by
--   ``mut.server.handlers`` and persisted via
--   ``SupabaseAuditManager.record``. Any action outside this set
--   (legacy node-level audit, future non-MUT audit writers, etc.)
--   is left untouched.
--
-- SCOPE
--   All projects. Cost acceptable per the explicit product
--   decision to run a forward-only identity migration.
--
-- NOT DONE
--   We do not also DROP-and-ADD the project_id NOT NULL
--   constraint; the prior migration already normalized writers
--   onto project_id. This is purely a data cleanup.
-- ============================================================

BEGIN;

DELETE FROM audit_logs
WHERE action IN (
    'clone',
    'push',
    'push_rejected',
    'push_error',
    'merge_conflict',
    'pull',
    'pull_commit',
    'rollback',
    'rollback_error'
);

COMMIT;
