## Context
Generic Synchronize and Access contracts already exist in local qubits. Final acceptance also requires GitHub-specific bindings, one-time Database Import sources, aggregation and actual retirement/deployment evidence. Existing GitHub routes declare Project actions in the manifest but do not execute the PDP; OAuth row IDs are also not owner-checked at binding/discovery.

## Decisions
- Use thin HTTP projections over existing operations, not new repositories or lifecycle writers. Canonical clients never retry legacy URLs or reinterpret resource IDs.
- GitHub paths use `/projects/{project_id}/synchronize/github/{binding,repos,branches,pull,push,logs}` and `/synchronize/github/webhook`. Bindings use auto_pull/last_pulled_*/last_pushed_*; logs reference synchronize_github_binding_id with inbound/outbound direction. Preserve synchronous manual operations, raw-body HMAC, existing deduplication and webhook acknowledgement behavior.
- Database paths use `/imports/database/sources` and source-ID table/preview/save suffixes. Creation returns `{source, database_info}`. Save is an existing synchronous one-time operation, not a new ImportJob or ongoing binding. Source configuration never enters response DTOs. Storage/classification remains owned by 049.
- Project policy must execute, not merely appear in a manifest. OAuth ownership is checked when attaching/discovering an account; a Project's already-bound source follows Project permissions rather than pretending every caller owns the original OAuth account.
- Query/body validation rejects unknown, repeated and blank selectors before dispatch. Do not recursively rename user metadata, historical audit content or provider data.
- Retirement is a distinct evidence gate. Source implementation and isolated tests cannot establish cessation of real old producers, clients or webhook configuration.

## Migration and recovery
Complete canonical API/client code, test compatible schema/service behavior, integrate approved schema/runtime/CLI deliveries, then rehearse coordinated backend-first cutover. Remove old contracts only with consumer/queue/configuration evidence. Recovery must use a version compatible with final schema and preserve post-cutover data. No remote push or production operation without explicit scope approval.
