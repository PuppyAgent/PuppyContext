# CLI Bug Backlog

Recorded during the filesystem/access CLI review. These are intentionally
not fixed in the filesystem concurrency patch.

## Gateway output helpers

- `gateway providers` and `gateway ls` pass array rows/columns to
  `out.table()`, but `out.table()` expects object rows and `{ key, label }`
  column descriptors.
- Several `gateway` commands call `out.success()` with arrays or strings.
  `out.success()` currently only emits valid JSON for object payloads.

## Auth option handling

- `auth whoami --api-url --api-key` reads only saved config, so command-line
  auth overrides are ignored.
- `auth whoami` human output labels the account as `User:` rather than
  exposing an `email` field, while `cli/tests/run.sh` expects "email".

## Datasource routing — source-tree fix, not released

ISSUE-060 removes gateway autodetection from external-source creation. Import
and Synchronize query their own server-admitted Provider views; Access lists
Agent/MCP/Sandbox kinds. The CLI HTTP regression executes all three owning
services. Generic Synchronize and Access use canonical paths without fallback;
GitHub/database-source cutovers and publication remain pending; see
[unreleased compatibility notes](../docs/cli/ENTRYPOINTS-UNRELEASED.md).

## Agent create config — ignored options now rejected

`access add agent --model/--system-prompt` and model/prompt create config now
fail before requests. The current Access Agent create path consumes neither
`model`, `llm_model` nor `system_prompt`; changing the spelling would not fix it.
No model/prompt configuration feature is claimed. Use supported Agent workflows;
these CLI flags must not silently create a default-configured Agent.
