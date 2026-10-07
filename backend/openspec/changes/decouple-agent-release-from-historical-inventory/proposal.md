# Change: Decouple Cloud Agent releases from historical data repair

## Why
A whole-inventory migration prevented acceptance of a readable Agent project because unrelated historical projects could not migrate. The user explicitly approved bounded release acceptance on 2026-10-08 and rejected all-project auditing as an ordinary CI/CD gate.

## What Changes
- Add an owner-only activation operation for explicitly selected prepared projects.
- Bind an immutable Qubits rollout artifact and receipt to the selected project.
- Use one current-tree usage baseline for the affected organization; do not read sibling history.
- Keep global recovery/archive Contract independent, with its existing stronger gates.

## Impact
- Affected specs: agent-chat release acceptance.
- Affected code: additive schema, immutable operator artifact, migration tests and release docs.
- Unselected projects retain their source and require separate adoption; this is not a whole-environment readiness claim.
