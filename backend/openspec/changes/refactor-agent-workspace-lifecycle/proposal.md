# Change: Isolate Agent workspaces and publish once per run

## Why
Conversation checkpoints currently collect every file and repack the complete
Git history, even around read-only tools. The supervisor implements a second
Git publication path. This amplifies CPU, network and database work and makes
recovery depend on unrelated storage responsibilities.

## What Changes
- Retain configured Agent identities and the durable Run API.
- Separate conversation state, provider workspace recovery, Git synchronization
  and publication from run orchestration.
- Use stock Git and the existing Git transport with run-bound publication;
  remove Agent object enumeration and project S3 access.
- Reuse unchanged recovery points for read-only tools; publish only on successful
  run completion, with no empty commits and no repeated commits after retries.
- Exercise real sandbox/Git/storage and failure/request-budget contracts.

## Approval
The user accepted these boundaries and explicitly requested implementation on
2026-10-08. Work is isolated; merge and deployment remain deferred.

## Impact
- agent-chat; sandbox artifact/control protocol; Git transport admission.
- Existing v1 execution checkpoints are not an alternate runtime protocol.
