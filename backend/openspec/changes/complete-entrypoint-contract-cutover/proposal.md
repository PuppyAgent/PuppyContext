# Change: Complete entrypoint resource contracts and acceptance

## Why
User-approved ISSUE-058 §§5.4–5.7 and ISSUE-059 require full resource/consumer cutover and joint acceptance, not closure after isolated Access/Synchronize increments. GitHub, Database Import, aggregate identities and compatibility retirement remain outstanding.

## What Changes
- **BREAKING** Publish canonical GitHub binding/pull/push/log/discovery/webhook and Database Import source contracts; migrate Web/shared SDK consumers without fallback.
- Enforce actual Project actions and OAuth ownership on GitHub routes, including transitional routes; align Database Import source-management authorization with its manifest.
- **BREAKING** Replace ambiguous Dashboard/Activity resource envelopes and synchronize permission wire values, preserving role semantics and explicit domain identities.
- Complete cross-resource identity/lifecycle tests and inventory every remaining old contract/consumer, with evidence-based retirement rather than permanent aliases.
- Integrate reviewed delivery from 049/053/054/060/061 and verify joint schema/client/runtime behavior. Production operations, deployment and remote mutations still require explicit authorization.

## Impact
- Affected specs: context-entrypoints.
- API/router/authorization boundaries, Desktop/Web/shared resource consumers, contract/security/HTTP tests and canonical documentation.
- No duplicate schema/classification or worker/CLI implementation: 049 and 060/061 retain ownership. No database DDL, provider or Version Engine rewrite is authorized by this proposal.
