## Implementation

- [x] Replace production-before-merge checks with staging evidence and Docker rehearsal.
- [x] Implement a shared ordered upgrade executor and durable private journal.
- [x] Add Docker and hosted release adapters with explicit deployment ownership.
- [x] Test phase failure, retry, exact source deployment and real PostgreSQL fencing.
- [ ] Complete populated main-to-candidate Docker upgrade and acceptance.
- [ ] Verify fresh installation and current Qubits-to-candidate upgrade.
- [ ] Update canonical architecture and validate documentation/specs.
- [ ] Complete focused regression checks and prepare the reviewable change.

## Activation boundary

Main merge, production migration and hosted runner/service configuration are
separate deployment actions. No activation is claimed by these checkboxes.
