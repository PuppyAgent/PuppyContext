# Repair missing Project root Scopes

Staging holds historical Projects whose creation committed the Project row
without its single root Scope (`repo_scopes` row with `path = ''`,
`is_root = true`). Every such Project fails the fail-closed readiness audit in
`20260715_project_owned_repository_targets_preflight`, which blocks the
Project-owned repository target cutover and therefore every schema deployment
behind it.

`run.sql` restores exactly the row the application would have created
(`Root`, `path=''`, `exclude='[]'`, `mode='rw'`, `is_root=true`) for Projects
with no root Scope, and records one audit fact per repaired Project. It fails
closed without mutating anything when a non-root Scope already claims the
root path. `verify.sql` fails closed while any Project lacks exactly one root
Scope and reports the missing/surplus split, so a `plan`/`run` dispatch also
serves as the sanctioned diagnosis of the protected environment.

Release order: run this repair, then run the
`20260715_project_owned_repository_targets_preflight` data migration, then
deploy schema.
