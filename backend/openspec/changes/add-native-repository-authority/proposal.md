# Change: Add native repository authority (ISSUE-062)

## Why

Root-first publication and named-ref upserts do not provide uniform Git ref
transactions. The accepted ISSUE-062 design retains S3/PostgreSQL and the
Puppyone Version Engine while making objects, refs and HEAD canonical.

User authorized implementation in isolated worktrees and local integration into
`qubits` only after tests pass. This does not authorize remote publication,
production DDL or migration of user repositories.

## What Changes

- First establish executable legacy/target tests, native Git comparison and
  isolated database evidence. Preserve existing failing targets as failures in
  strict acceptance, not as supported capabilities.
- Share typed graph rules between transport and GC: byte-preserving names,
  ordered parents, tag edges, external gitlinks, and fail-closed corruption.
- Subsequently add repository metadata, durable receipts/pins, unified ref
  transactions, consumers, migration and recovery according to M01–M20.
- **BREAKING (future, gated):** native-repository authority replaces root-first
  authority only for repositories that pass the migration gates. Old API and
  scoped projections remain explicitly bound compatibility contracts.

## Impact

- Affected specs: `git-object-graph`, future `git-repository-authority`.
- Affected code: `version_engine/write_engine`, storage, transport, GC, database
  migrations, product writers, Workspace and Desktop consumers.
- No provider/entrypoint renaming or shared SQL migration is duplicated here.
- `replace-handrolled-git-transport-with-official-git-backend` still describes
  legacy transport. Its single-main/linear restrictions are not the native
  profile's target; they are NOT removed until the new publication gates pass.

## Source of requirements

[ISSUE-062](https://github.com/puppyone-ai/puppy-issues/tree/main/dev%20issues/0-discussing/ISSUE-062-cloud-native-git-repository)
owns M01–M20, the live-data plan (07) and test requirements (08). This change
records implementation, not a replacement acceptance scope or a completed
migration claim.
