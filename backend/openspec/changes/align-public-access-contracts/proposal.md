# Change: Align public Access surface contracts

## Why

The accepted ISSUE-058 resource map requires `/access/surfaces` and project-scoped AccessSurface APIs. Generic Synchronize has now moved to canonical clients, but Access still exposes Connector/Connection DTOs and ambiguous provider/run fields. ISSUE-060 is waiting for this contract.

## What Changes

- Add canonical global and project-scoped Access surface routes, typed requests/responses and named Project authorization.
- Reuse the existing Access services, adapters, target validation and single access_surfaces persistence layer. No mirrored records or second credential authority.
- **BREAKING for migrated clients:** use `kind`, AccessSurface types and access_surface_id references; remove obsolete ConnectorRun/source-run consumer shapes. User-owned config is not recursively renamed.
- Preserve Project-root/Scope contract v2, kind-specific create policy, entitlement checks and explicit credential issuance. Ordinary reads/metadata responses must not disclose credentials; metadata writes must not become another credential-issuance API.
- Migrate Desktop/Web/cloud-core leaf clients without URL/ID fallback. Preserve genuine product orchestration and Access protocol names.
- Retain old HTTP only for the coordinated S2/S3 window; final retirement remains open.

## Approval and scope

This implements the user's approved ISSUE-058 sections 5.4/5.7 and repeated instruction to continue in isolated worktrees, verify, and integrate into local qubits. Database/schema classification belongs to 049, CLI to 060, runtime/jobs to 061, and Git/storage to 062. No remote push, deployment, production query or migration is authorized.

## Impact

Backend Access HTTP/schema boundary, main router ordering, authorization manifest, credential/target/identity tests, Desktop/Web/shared SDK resource clients, canonical documentation and audits. GitHub and Database Import public contracts remain separate pending work; this increment cannot close 058/059 on its own.
