# Change: Cloud Agent Git workspace

## Why
Cloud knowledge-writing runs currently materialize file snapshots, so the Agent
cannot inspect repository history or preserve commits it creates. The user has
approved clone, edit, automatic commit and publication on the cloud branch,
implemented in a backend worktree and verified before merging into qubits.

## What Changes
- Clone a full native Project repository inside the provider sandbox.
- Automatically commit each completed turn and publish its original Git objects.
- Retain Git history, index and dirty files in durable recovery checkpoints.
- Use existing run authorization, stop/fence and native ref transaction checks.

## Impact
- Affected capability: agent-chat; worker artifact, supervisor and publication.
- Native restricted views remain unavailable until their Git projection exists.
- Hosted E2B validation is separate from local verification and source delivery.
