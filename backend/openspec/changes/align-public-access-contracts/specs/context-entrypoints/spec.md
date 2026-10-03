## ADDED Requirements

### Requirement: Canonical Access surface contract
Global and project-scoped Access HTTP APIs SHALL use AccessSurface DTOs, kind and explicit access_surface_id references. They SHALL reuse the owning Access operations and persistence, not create parallel resource records.

#### Scenario: Same resource through either inventory
- **WHEN** an authorized caller reloads a surface through global or Project inventory
- **THEN** its id, project_id, kind and RepositoryTarget identify the same stored Access surface
- **AND** the client never substitutes a Synchronize binding ID or infers a Project root from an invalid Scope

#### Scenario: Legacy route ordering
- **WHEN** a caller requests `/access/surfaces` or its static kind catalog
- **THEN** canonical routes resolve before legacy dynamic connection-ID routes

### Requirement: Preserve Access authority and credential boundaries
Access APIs MUST preserve ProjectGrant, contract-v2 RepositoryTarget and kind-specific authorization/entitlement rules. Credentials MUST be disclosed only by explicit authorized issuance responses, never by metadata serialization.

#### Scenario: Viewer or foreign target
- **WHEN** a Viewer manages credentials or a caller targets an unauthorized Project/foreign Scope
- **THEN** the operation is denied before persistence or issuance
- **AND** a runtime credential cannot replace a Human ProjectGrant

#### Scenario: Metadata credential smuggling
- **WHEN** metadata contains nested credential-bearing config keys
- **THEN** ordinary responses redact them and metadata mutation rejects issuance attempts
- **AND** legacy Project routes do not provide a credential-leaking bypass

#### Scenario: Legacy source row
- **WHEN** a historical external-source or import row is encountered
- **THEN** it is not presented or executed as an ongoing Access surface
- **AND** direct management returns an actionable error after authorization rather than dispatching Synchronize or guessing a migration

### Requirement: Canonical Access consumers
Desktop, Web and shared SDK Access leaf clients SHALL use canonical routes and types without fallback. Product composition and actual protocol names MAY remain above resource clients.

#### Scenario: Canonical resource identity
- **WHEN** a migrated client lists, edits, pauses, resumes, activates or deletes a surface
- **THEN** it uses the same access_surface_id and explicit target context
- **AND** obsolete ConnectorRun/source-run DTOs are not treated as Access lifecycle contracts

#### Scenario: Staged cutover
- **WHEN** migrated clients are enabled
- **THEN** the canonical backend must already exist
- **AND** old server routes remain transitional only; final issue acceptance still requires consumer retirement, migration and deployment evidence
