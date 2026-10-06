# Replace the hosted Agent execution runtime

## Why
The request-owned Python model loops cannot survive a disconnected client and can report success after publication fails. Configured Agents must keep their identity, prompt, model, visibility and bindings while execution moves to a managed Pi sandbox.

## What Changes
- **BREAKING**: replace POST /agents with durable submit/query/stop/approval and cursor-based event APIs. No legacy execution or protocol compatibility layer.
- Reuse access_surfaces, access_tools, chat_sessions, project authorization, managed AI billing, sandbox providers and the canonical Version Engine.
- Add independent run leases, fenced writes, immutable checkpoints and conservative recovery of uncertain effects.
- Move scheduled Agent execution to the same submission service.

## Approval and scope
The user explicitly approved this replacement, a fresh backend worktree and local integration into qubits after validation on 2026-10-06. ISSUE-109/110/111 are the backend acceptance owners. Desktop/Web cutover remains ISSUE-112/113. No remote deployment or push is included.

## Impact
Cloud clients must adopt the run protocol. Existing Agent configuration is retained. Existing display history is retained as data; it is not used as a Pi execution checkpoint.
