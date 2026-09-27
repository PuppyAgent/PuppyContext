# Change: Align entrypoint persistence after ISSUE-048

## Why
The accepted four-entrypoint architecture still writes legacy Access/GitHub names
and stores search-index jobs in uploads. ISSUE-049 authorizes the database phase
in an isolated worktree, with no merge or hosted deployment.

## What Changes
- Add canonical Access and GitHub identifiers without breaking old processes.
- Rename the GitHub binding table with an invoker compatibility view over the
  same records. Move search tasks to dedicated storage with transactional mirrors.
- Backfill through the existing immutable data runner, then use canonical storage
  in backend repositories and retain public response spellings at serialization.
- Prepare a separately promoted Contract for removing transition objects, guarded
  by both the exact artifact receipt and live row equality. Do not activate it in
  the same release as Expand.

## Impact
Cloud schema/data artifacts, backend repositories, migration CI, and issue docs.
No client, Pay, baseline, archived migration, shared database or Railway changes.
