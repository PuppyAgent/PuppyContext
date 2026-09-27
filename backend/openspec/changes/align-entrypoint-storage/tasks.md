## Implementation
- [x] Add compatible schema and immutable SQL backfill/verification artifacts.
- [x] Switch repositories and dashboard to canonical storage; preserve public JSON.
- [x] Emit the canonical GitHub ARQ job name, preserving queue and dedup identity.
- [x] Prepare a separately promoted, guarded cleanup Contract.
- [ ] Complete real Supabase fresh/upgrade/cleanup and installation rehearsals.
- [ ] Document exact validation evidence and remaining operational gates.

## Separate cleanup release
- [ ] Verify old producers/processes and delayed/retry jobs have drained.
- [ ] Promote the reviewed Contract in a later PR and remove the old dispatcher.
- [ ] Resolve source-shaped Access and gateway records using verified data/consumer evidence.
