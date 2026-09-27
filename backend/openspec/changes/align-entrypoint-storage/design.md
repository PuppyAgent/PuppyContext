# Storage and deployment boundary

Release A installs compatible schema, runs the backfill, then starts the new
application after CI succeeds. Both old and new processes operate on the same
GitHub binding records. Paired identifier triggers reject conflicting IDs.
Search task mirrors run in the same transaction; conditional upserts stop trigger
recursion, and a failure rolls back both interfaces. Historical fields, including
unknown config/result keys, timestamps and cancelled states, are preserved.

Release B is separate: confirm old processes and serialized GitHub jobs have
left the system, end the old-application rollback window, verify the data job on
both environments, and promote contract.pending.sql unchanged. Under a bounded
exclusive lock it checks the receipt and current data again, replaces SQL body
references, removes aliases and search mirror rows, and rejects future old writes.
A fresh empty install can apply the Contract without a historical data receipt.

Access/GitHub/search task storage is backend-only: RLS plus explicit service-role
DML grants; anon/authenticated have no direct table privileges. The legacy view
uses security_invoker and cannot widen those privileges.

The database cannot prove that external workers have exited. SQL data checks do
not replace the deployment/queue-drain gate. No timestamp or fabricated receipt
is used as a substitute for that operational evidence.
