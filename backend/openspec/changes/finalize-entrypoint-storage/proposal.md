# Change: Finish the accepted ISSUE-049 storage and ISSUE-058 retirement contract

## Why
Canonical consumers are integrated but Database Import still writes the binding store. The owner is now taking over unassigned 049 under the user's explicit instruction to finish the entire accepted scope, rather than stopping at a handoff.

## What Changes
- **BREAKING**: deliver the existing final table/field mapping, independent Database Import storage, and remove old runtime storage/HTTP aliases after cutover.
- Preserve immutable B1, earlier migrations and data receipts. Deliver one 049 migration sequence, not parallel SQL.
- Add an Expand/data release with explicit, fingerprinted row decisions; never classify by provider/manual mode alone. Final Contract is a separate release after the data and consumer gates.
- Preserve IDs, encrypted credentials, tenant/target boundaries and run history. Explicit dual-use rows retain a binding and an independently identified Import source relation. Unsupported historical bindings can be explicitly retained read-only, never automatically reactivated.
- Reuse 061 real local stack and queue scanners; test PostgreSQL upgrades, actual clients and post-contract recovery. No remote operation follows merely from implementation approval.

## Impact
- Specs: context-entrypoints, database release workflow.
- Code: supabase migrations/data artifacts, repositories/direct consumers, resource transports, local release acceptance and canonical documentation.
- Unchanged: 062 Git schema/engine, 054's broader governance program, private billing, OAuth protocol and user/Version history.
