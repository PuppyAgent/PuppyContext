## ADDED Requirements

### Requirement: GitHub Synchronize resource boundary
GitHub-specific bindings and logs SHALL use canonical Synchronize URLs, DTOs, field references and inbound/outbound directions without becoming generic bindings or ImportJobs. Manual execution, watermark and webhook deduplication semantics SHALL remain unchanged.

#### Scenario: Canonical lifecycle
- **WHEN** an authorized client creates, reloads, updates, pulls, pushes or deletes a GitHub binding and reads logs
- **THEN** responses use kind-correct resource references and canonical fields, not integration_id or import/export direction aliases

#### Scenario: Real authorization and ownership
- **WHEN** a Viewer, foreign Project user or caller selecting another user's OAuth row attempts management/discovery
- **THEN** actual Project/OAuth checks reject it before persistence or provider access on both canonical and transitional routes

#### Scenario: Signed webhook
- **WHEN** GitHub sends raw signed bytes or repeats a delivery at the canonical webhook URL
- **THEN** existing HMAC, branch and replay checks apply before dispatch and response references use synchronize_github_binding_id

### Requirement: Database Import source boundary
One-time database source management SHALL use ImportDatabaseSource resources with explicit Project ownership, canonical source paths and IMPORT_SOURCE_MANAGE authorization. It SHALL NOT fabricate an ongoing Synchronize or Access relationship.

#### Scenario: Source lifecycle and one-time save
- **WHEN** an authorized user creates/lists/deletes a source, lists/previews tables or saves one table
- **THEN** source envelopes exclude credentials and save retains its existing one-time behavior and exact target Project

#### Scenario: Malformed or cross-domain selector
- **WHEN** a caller supplies legacy/unknown/repeated/blank selectors, a foreign source or another resource domain's ID
- **THEN** the request fails before broadening selection, exposing secrets or writing content

### Requirement: Domain-qualified resource aggregation
Dashboard resources SHALL retain explicit resource kind, resource ID and Project ownership. Access targets SHALL derive from persisted surface/Scope facts, not Provider, name or path matching. Activity SHALL emit synchronize_run without rewriting its own IDs or historical text. Synchronize permission spelling SHALL change without role escalation.

#### Scenario: Equal resource IDs across domains
- **WHEN** an Access surface and Synchronize binding have the same ID, name or destination
- **THEN** inventory, statistics, navigation, pause/delete and execution remain scoped to the explicitly selected resource domain

#### Scenario: Failed or obsolete inventory
- **WHEN** canonical inventory reads fail or a client receives only legacy connections or sync_run aliases
- **THEN** the failure remains visible and the client does not fabricate an empty successful inventory or retry a legacy URL

#### Scenario: Stale user context
- **WHEN** Project, source, user/session or component lifetime changes during an asynchronous request
- **THEN** delayed completions cannot update the new context or attach the previous resource to it

### Requirement: Joint closure evidence
Final closure SHALL require every original ISSUE-058 and ISSUE-059 criterion, including actual consumer/queue/configuration retirement, schema/data verification and version-specific delivery evidence. Local test doubles SHALL be identified as such.

#### Scenario: Missing external acceptance
- **WHEN** source tests pass but schema classification, real old-consumer exit or environment authorization is absent
- **THEN** code may be locally integrated but the affected criteria and compatibility retirement remain unverified, not falsely closed
