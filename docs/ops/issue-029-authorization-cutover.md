# ISSUE-029 Authorization Cutover Runbook

This runbook deploys the final Project authorization and explicit workspace
binding architecture.  It does not operate a permanent legacy/v2 dual-allow
mode.  Any ambiguous data blocks the cutover.

## Release artifacts

- `20260712010000_expand_unified_project_authorization.sql`
- `supabase/archive/before_b1/data_migrations/20260712_repo_user_permissions_to_project_members`
- reviewed `contract.pending.sql` promoted only after both environment receipts
- canonical backend Project policy and route manifest
- binding/readiness APIs
- capability-driven Web and Desktop clients
- pgTAP database contracts and architecture tests

## Before the maintenance window

1. Take a recoverable database snapshot and record its identifier.
2. Record the application and Desktop release commit IDs.
3. Run a migration dry-run against staging and production.
4. On a staging clone, rebuild from migration zero and run the fresh/legacy
   database paths in `Validate Database Changes`.
5. Capture the preflight report:

   ```sql
   select jsonb_pretty(public.unified_authorization_preflight());
   ```

6. Abort unless every integrity count is zero.  In particular, do not waive:
   `legacy_denied`, `legacy_scoped`, `legacy_tenant_mismatch`, unknown roles,
   creator/Admin mismatches, root-scope defects, orphan surfaces/credentials,
   or invalid Agent/tool bindings.

`denied` and folder-scoped legacy human grants require an explicit product/data
decision.  They are never silently widened or discarded.

## Deployment order

1. Stop membership, Project, Access, and binding mutations or place the API in
   a short maintenance/read-only window.
2. Deploy the additive foundation migration to Qubits and Production.
3. Run the protected data migration in Qubits, verify it, then run and verify
   the identical artifact in Production. It deterministically maps
   `admin -> admin`, `editor -> editor`, and `reader -> viewer` and rejects
   conflicting or ambiguous rows.
4. Deploy the backend cutover before clients and wait for old instances to
   drain. Confirm `/health`, Project list/detail,
   authorization, binding, and readiness endpoints.
5. In a separate `db/contract-*` PR, copy the reviewed pending Contract into a
   newly timestamped `supabase/migrations/*_contract_*.sql` file. Run staging
   and production verification workflows before Main Release Gate permits it.
6. Deploy Web and Desktop clients that consume server capabilities.
7. Re-enable mutations after the Contract checks and product matrix pass.

Project + creator Admin and binding + one-time credential are independently
atomic database publications.  Do not split their RPC calls into client-side
multi-write sequences.

## Cutover gates

Run these checks immediately after database migration and again after backend
deployment:

```sql
select to_regclass('public.repo_user_permissions') is null
  as legacy_permission_removed;

select public.unified_authorization_preflight();

select count(*) from public.projects p
left join public.project_members pm
  on pm.project_id = p.id and pm.user_id = p.created_by
where p.created_by is not null and pm.role is distinct from 'admin';

select count(*) from public.access_surface_credentials c
left join public.access_surfaces s
  on s.id = c.access_surface_id
 and s.project_id = c.project_id
 and s.org_id = c.org_id
where s.id is null;
```

All results must be true/zero.  Then verify the product matrix:

- org-visible non-member is Viewer and cannot mutate;
- private non-member receives non-disclosing not-found;
- explicit Editor can write/run but cannot manage members, surfaces, or keys;
- Admin can manage the Project;
- Project list contains only accessible Projects and includes capabilities;
- Viewer binding is `r`; Editor/Admin binding can be `rw`;
- rotating or revoking one binding does not affect another;
- Editor-to-Viewer and scope-rw-to-r revoke the old `rw` credential;
- non-root binding is scoped and cannot unlock Claude;
- Product/API root edit does not unlock Claude;
- the first committed root Git push does unlock Claude.

## Observability

Watch structured `project_authorization_decision` events grouped by action,
outcome, reason, role, and grant source.  Project references are redacted and
user identifiers are omitted.  Alert on:

- fact-store-unavailable warnings;
- unexpected deny spikes for Project read or content write;
- credential resolution after revoke/downgrade;
- canonical Git locator mismatch/wrong-host increases;
- readiness reporting ready without a committed root `access_git` transaction;
- migration preflight counts becoming non-zero.

Never log raw JWTs, Git credentials, access keys, local paths, Project
names, or file content while investigating.

## Abort and rollback

### Before legacy table retirement

The foundation migration is additive. Stop the data workflow, keep the
dual-compatible application version, repair the reported data, and rerun the
idempotent migration and preflight. Do not enable a permissive
`old_allow OR new_allow` fallback.

### After legacy table retirement, before client rollout

Keep the new database policy and roll application pods forward to the last
known-good build that understands ProjectGrant.  Recreating
`repo_user_permissions` would restore a second truth source and is not an
application rollback.

### Catastrophic database rollback

Only if data integrity or availability cannot be repaired forward:

1. stop all writes;
2. restore the recorded pre-cutover database snapshot;
3. redeploy the matching pre-cutover application commit;
4. invalidate credentials issued after the snapshot;
5. reconcile Git/Version Engine writes accepted after the snapshot before
   reopening writes.

Snapshot restore loses post-snapshot database facts and therefore requires an
incident, explicit reconciliation, and user communication.  It is not the
normal response to a client/UI defect.

## Completion evidence

Archive the preflight JSON, migration dry-runs, pgTAP output, backend/Desktop
test summaries, Web/Desktop builds, route-manifest result, release commits, and
post-deploy matrix results with ISSUE-029.
