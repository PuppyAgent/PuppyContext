# Agent Git workspace

Create the provider resource first, then clone inside it. The existing framed
provider channel carries a standard, self-contained Git bundle captured under
the canonical native read pin. This preserves all reachable history and refs
without putting a cloud write credential or internet access in model commands.
There is no backend checkout or second repository authority. Git bundle is a
transport format; the canonical native object store and ref transaction remain
the source of truth. This is an internal implementation choice for the approved
clone/edit/commit/publish workflow.

The immutable initial checkpoint contains the complete bundle and fixes the
target branch, object format, generation, base OID and HEAD guard. Full native
Project-root views are admitted; partial views reject before sending history.
The worker restores Git separately from knowledge files. Checkpoints preserve
refs, index, working files and executable modes; .git is never a content file.

After a successful turn, a trusted provider command freezes Agent processes,
stages intended working-tree changes and commits. Its bundle and original tip
are durable before publication. The backend validates ancestry and submits the
original Git objects through RefTransactionService with the existing run lease
and stable request UUID. It does not regenerate commits from a file patch or
force-update a concurrent branch. Stop/failure captures without auto-commit.

The first version retains bounded checkpoint limits and per-execution sandboxes.
No website build/deployment or user-facing Git workflow is introduced.
