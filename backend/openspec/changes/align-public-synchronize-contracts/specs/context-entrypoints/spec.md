## ADDED Requirements

### Requirement: Canonical generic Synchronize HTTP contract
The API SHALL expose generic bindings and runs under `/api/v1/synchronize`, with canonical resource references and typed response envelopes. Provider config and historical payloads MUST NOT be recursively renamed.

#### Scenario: Create, reload and manage one binding
- **WHEN** an authorized administrator creates a binding with project_id, provider, config and target_path
- **THEN** POST `/synchronize/bindings` returns `binding` and an optional execution_result containing synchronize_binding_id and synchronize_run_id
- **AND** GET `/synchronize/bindings?project_id=...`, binding PATCH/DELETE, trigger/pause/resume/refresh/runs and GET `/synchronize/runs/{run_id}` refer to that same binding
- **AND** binding responses use last_synchronize_commit_id and run responses use synchronize_binding_id, never access_point_id

#### Scenario: Old identity field cannot broaden execution
- **WHEN** a canonical request contains connection_id, access_point_id or sync_id instead of synchronize_binding_id
- **THEN** it is rejected before mutation, not silently interpreted as a project-wide operation

### Requirement: Preserve authorization and lifecycle semantics
Canonical routes SHALL reuse the existing Synchronize application operations, credential redaction, target validation and Project authorization. Legacy HTTP compatibility SHALL NOT create another lifecycle implementation.

#### Scenario: Foreign or Access identity
- **WHEN** a caller supplies an Access ID, missing binding, foreign binding or foreign run
- **THEN** authorization/not-found handling prevents data disclosure or mutation of an unrelated resource

#### Scenario: Legacy mixed source storage
- **WHEN** the transitional repository returns an unclassified record with Database Import indicators or an import_once trigger
- **THEN** binding inventory/management and pull return 409 SOURCE_CLASSIFICATION_REQUIRED rather than expose credentials or mutate it as a binding
- **AND** bulk pull validates all candidates before enqueueing any work
- **AND** no provider-name heuristic is treated as proof of ownership or an instruction to move/delete data; ISSUE-049 owns that classification and migration

#### Scenario: Queue unavailable
- **WHEN** the initial enqueue fails
- **THEN** the existing cleanup/error behavior is preserved and no successful creation is manufactured by the canonical response adapter

### Requirement: Canonical resource consumers
Desktop, Web and shared SDK generic Synchronize clients SHALL use canonical paths and resource DTOs without runtime fallback. Automation product names may remain above resource clients.

#### Scenario: Explicit config payload boundary
- **WHEN** source metadata contains a user-owned field named connection_id
- **THEN** it remains unchanged; only documented resource envelope/reference fields are converted

#### Scenario: Transitional release
- **WHEN** S2/S3 consumers migrate
- **THEN** old HTTP consumers may temporarily use the existing transport, but the new clients never emit its paths
- **AND** ISSUE-058 remains open until final retirement, migration and environment evidence is complete
