# Single native Git authority

User approval: the 2026-10-07 instruction to implement the agreed migration and
remove the former protocol concept is approval of this implementation scope.

PostgreSQL owns repository refs and publication admission; S3 owns canonical
Git objects. Historical snapshots without Git identity become labelled imports.
Private recovery data is archived without public refs. Source retention is not
permission to expose private objects through Git transport.

Changes follow Expand -> immutable data artifact -> consumer cutover -> gated
Contract. The Contract is staged separately from the additive release. Historical
SQL is not edited to hide its provenance. Only operator migration tools may know
old physical names; application code must not read the retired namespace.

Authorization remains explicit: human Project grants and machine Runtime grants
are not interchangeable. New Scope projections are outside this cutover; a
retained Scope credential cannot turn into a whole-repository credential.

Validation uses owned local Docker PostgreSQL and S3. Never inherit hosted
credentials or write the cloud test database. External execution waits for the
reviewed Qubits deployment and maintenance window.
