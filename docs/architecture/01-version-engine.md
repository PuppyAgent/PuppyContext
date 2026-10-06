# PuppyOne Version Engine

Native full-Project repositories now use PG refs and direct S3 object transport;
see [Native Git object transport](17-native-git-object-transport.md). They do not
reconstruct a local bare repository. The root/projection architecture below
also describes the retained legacy profile; it is not the native ref authority.

PuppyOne is now Git-native at the version layer. Product features such as
scope boundaries, optimistic merge, hosted conflict review, audit, projection,
and outbox repair live above Git in the Version Engine. The server does not
maintain a second version-control protocol.

Repository identity follows
[Project-Owned Repository Targets](15-project-owned-repository-targets.md):
Project is the sole root repository identity, while every persisted Scope is a
non-empty path boundary. “Root” below names an empty-path Project projection,
never a synthetic Scope resource.

## Architecture

```text
                         Product Write Surfaces
                         ======================

  Web editor / uploads      Puppyone CLI FS        Git smart HTTP
  sync connectors           agent/sandbox writes   clone/fetch/push
        |                         |                     |
        v                         v                     v
  +------------------+     +------------------+   +-------------------+
  | Content routers  |     | ProductOperation |   | Git transport     |
  | ingest/finalize  | --> | Adapter          |   | upload/receive    |
  | connector jobs   |     |                  |   | pack + quarantine |
  +---------+--------+     +---------+--------+   +---------+---------+
            |                        |                      |
            | OperationWriteIntent   | OperationWriteIntent | VersionSubmissionIntent
            +------------------------+----------------------+
                                     |
                                     v
  +------------------------------------------------------------------+
  | Version Engine: write_engine/engine.py                             |
  |                                                                  |
  | - validates actor, access point, scope, excludes, and base state  |
  | - applies product splices or accepted Git trees to root           |
  | - retries with root CAS and path-conflict checks                  |
  | - runs merge policy: auto merge, LWW, manual review, reject       |
  | - creates canonical Git commit/tree/blob facts                    |
  | - atomically publishes refs, history, audit, transaction, outbox  |
  +-------------------------------+----------------------------------+
                                  |
              +-------------------+-------------------+
              |                                       |
              v                                       v
  +---------------------------+       +-------------------------------+
  | Git object storage        |       | Supabase control plane        |
  |                           |       |                               |
  | version/<project>/objects |       | repository_scopes             |
  | version/<project>/bundles |       | canonical projects root column|
  | blob/tree/commit bytes    |       | scope-state and commit rows   |
  | object-location index     |       | conflicts, transactions       |
  | transport cache           |       | audit logs, durable outbox    |
  +-------------+-------------+       +---------------+---------------+
                |                                     |
                v                                     v
  +---------------------------------------------------------------+
  | Read / derived consumers                                      |
  |                                                               |
  | Web tree/history/diff, Git clone/fetch, search indexing,      |
  | notifications, conflict dashboard, object GC, sync exports.   |
  +---------------------------------------------------------------+
```

## Canonical Layered Flow Map

This is the current routing map. Protocol surfaces stay separate, while command
construction, transaction semantics, audit, conflict, write-system follow-up, and
physical object storage converge below the protocol boundary.

## Source Of Truth

The Version Engine's long-term invariant is root-first:

```text
Project canonical root = the single source of truth.
Scopes = path-bounded access wrappers over that root.
Scope-state rows = derived/cache/compatibility state, never authority.
```

Concretely, the project root hash column (`projects.version_root_hash`, formerly
`projects.version_root_hash`) is the canonical tree for the project. Product
writes, Access Point writes, Git pushes, CLI FS writes, and connector writes all
land by applying a path-scoped patch to that root and conditionally publishing a
new root. `repository_scopes` defines optional path boundaries; it does not define a
separate project truth.

This is the same architectural shape as Git-backed systems: one repository view
has one current root tree, while permissions, remotes, branch views, search
indexes, and UI caches are wrappers or derived views around that root. A derived
view may lag or be rebuilt, but it must never make the project appear empty or
replace the canonical root as truth.

### Legacy Scope-State Compatibility

Existing deployments may still expose the restricted `mut_scope_state` compatibility view and older scoped heads. The canonical table is `version_scope_state`. That
state is compatibility data during migration and may be used to rebuild or repair
the canonical root for legacy projects, but new root-first writes must not treat
per-scope heads as independent sources of truth.

Allowed uses for scope-state after the root-first cutover:

- fast lookup of the subtree hash under a scope path;
- compatibility reads for old projects until they are migrated;
- read-repair input when a legacy projection is missing or damaged;
- migration tooling that folds historical scope heads into one canonical root.

Forbidden uses:

- deciding that the project is empty because a derived/root projection is empty;
- publishing one scope head as authoritative without updating the canonical
  project root;
- letting a scope cache override a newer project root;
- treating multiple scope heads as the steady-state data model for new projects.

```text
Legend:
  [P] Product root write/read        [A] Access Point scoped write/read
  [G] Git-native transport          [B] Batch/internal tool write
  ---> synchronous request path      - - > post-commit follow-up

                                      L0 Client / Caller
       +--------------------+     +--------------------+     +--------------------+     +--------------------+
       | [P] Frontend Data  |     | [A] Puppyone CLI   |     | [G] Git CLI/native |     | [B] Ingest/MCP/    |
       | Page / Product UI  |     | fs write/rm/mv     |     | clone/fetch/push   |     | Sync jobs/tools    |
       +---------+----------+     +---------+----------+     +---------+----------+     +---------+----------+
                 |                          |                          |                          |
                 v                          v                          v                          v

                                    L1 Protocol Entry
       +--------------------+     +--------------------+     +--------------------+     +--------------------+
       | /api/v1/content    |     | /api/v1/ap-fs      |     | /git/*.git         |     | internal router /  |
       | content routers    |     | AP-FS router       |     | Smart HTTP         |     | ingest workers     |
       +---------+----------+     +---------+----------+     +---------+----------+     +---------+----------+
                 |                          |                          |                          |
                 v                          v                          v                          v

                           L2 Auth / Identity Resolution
       +--------------------+--------------------+--------------------+--------------------+
       | Product user auth  | AP/CLI key auth    | Git credential     | Connector/job auth |
       | JWT/session        | scope access key   | Basic/Bearer/key   | MCP/service key    |
       | membership         | revoke check       | user/scope binding | connector binding  |
       +--------------------+--------------------+--------------------+--------------------+
       | Output: AuthContext = actor + credential + project/scope/connector binding        |
       +-----------------------------------------------------------------------------------+
                                               |
                                               v

                              L3 Permission
       +--------------------+--------------------+--------------------+--------------------+
       | Product root       | AP/CLI scope       | Git remote scope   | Connector/job      |
       | role/can_write     | mode/excludes      | mode/excludes/ref  | target scope       |
       | canonical root     | connector status   | fetch/push allowed | batch policy       |
       +--------------------+--------------------+--------------------+--------------------+
       | Output: TargetAdmission = AuthContext + allowed target/actions/snapshot           |
       +-----------------------------------------------------------------------------------+
                            |                                         |
                            v                                         v

                                        L4 Intent Adapters
       +-----------------------------------------+-----------------------------------------+
       | Product / AP / batch adapter            | Git submission adapter                  |
       | from Product root + AP/CLI scope        | from Git remote + connector/job         |
       | VersionWriteCommandService helper       | receive-pack + quarantine               |
       | op command + TreePatch/splice_fn        | proposed tree + submission intent       |
       +-----------------------------------------+-----------------------------------------+
                            |                                         |
                            +--------------------+--------------------+
                                                 |
                                                 v

                                          L5 Write System
       +-----------------------------------------------------------------------------------+
       | +-----------------------------------------------------------------------------+ |
       | | L5 Core Write Engine                                                       | |
       | |                                                                             | |
       | | Goal: land one admitted write as durable Git-native                         | |
       | | version facts.                                                              | |
       | |                                                                             | |
       | | Inputs from L4:                                                             | |
       | |   Product/AP/batch -> OperationWriteIntent +                                | |
       | |     TreePatch/splice_fn                                                     | |
       | |   Git push -> VersionSubmissionIntent + proposed Git tree                   | |
       | |                                                                             | |
       | | Main path:                                                                  | |
       | |   Read current head/root                                                    | |
       | |     -> Build candidate version                                              | |
       | |     -> Store immutable blob/tree/commit objects                             | |
       | |     -> Try conditional root publish                                         | |
       | |                                                                             | |
       | | Conditional publish result:                                                 | |
       | |   accepted:                                                                 | |
       | |     write history/audit/ledger/outbox; return status=ok                     | |
       | |   rejected because head/root moved:                                         | |
       | |     read latest; resolve conflicts; loop to Main path                       | |
       | |   conflicts cannot be resolved synchronously:                               | |
       | |     write pending conflict; return status=pending                           | |
       | |   rejected because caller supplied stale expected head:                     | |
       | |     return status=conflict/409                                              | |
       | |   rejected after retry budget is exhausted:                                 | |
       | |     fail loud                                                               | |
       | | Conflict facts are created here, before any derived                          | |
       | | UI/index work.                                                              | |
       | |                                                                             | |
       | | Object-store calls and publish gate are write-engine                         | |
       | | internals on this path. Physical bytes live in L6.                           | |
       | | Transport cache is protocol cache only, not source of truth.                  | |
       | +-----------------------------------------------------------------------------+ |
       |                                                                                   |
       | +---------------------------------------+   +-----------------------------------+ |
       | | L5 Diff / TreeDelta                  |   | L5 Follow-up / Repair             | |
       | | Structural write diff: path/tree/blob |   | Consumes committed facts from     | |
       | | ChangeSets for scope checks, sparse  |   | L5 Core.                          | |
       | | merge, conflict policy, changed      |   |                                   | |
       | | paths, history facts, and audit.     |   | - hooks and durable outbox        | |
       | | Not a human semantic diff.           |   | - scope caches and root->AP views | |
       | |                                      |   | - Git view cache warming/repair   | |
       | |                                      |   | - path/search indexes             | |
       | |                                      |   | - websocket/read model refresh    | |
       | |                                      |   | - search event dispatch           | |
       | |                                      |   | - object GC                       | |
       | |                                      |   | - committed-version repair        | |
       | |                                      |   |                                   | |
       | |                                      |   | Must not publish refs or decide   | |
       | |                                      |   | merge policy.                     | |
       | +---------------------------------------+   +-----------------------------------+ |
       +--------------------------------------+--------------------------------------------+
                                              |
                                              | object bytes + object-location index;
                                              | follow-up may read/repair/GC
                                              v

                                  L6 Storage Substrate
       +-----------------------------------------------------------------------------------+
       | ObjectStore abstraction, S3/Supabase physical backends, bundle/chunk layout,      |
       | object-location index, hash verification, storage compatibility shims, raw bytes. |
       | L6 has no product policy, merge policy, ref authority, or protocol semantics.     |
       +-----------------------------------------------------------------------------------+
```

Updates from the previous diagram:

- There is no standalone "normalization layer." Request cleanup and protocol
  parsing live inside L4 intent adapters. L3 already decided the target and
  permission; L4 must not re-decide root vs scope, excludes, writable mode, or
  ref policy.
- Git-native transport no longer appears under `VersionWriteCommandService`.
  Product, AP-FS, and batch file writes may use that command helper inside the
  Product/AP/batch adapter. Git push has its own adapter path: receive-pack,
  quarantine, proposed tree, and changed-path extraction.
- Git object writes and conditional root publish are shown inside L5 Core because
  they are part of the write loop. There is no separate downstream publish stage
  that can "return" to the engine; a moved head/root loops back to the Main
  path with the latest state, while unresolved conflicts return `pending`.
- L5 is now the write system, with L5 Core as the synchronous write authority,
  L5 Diff / TreeDelta as the boxed structural write-diff module, and L5
  Follow-up / Repair on the right. L5 Follow-up consumes committed facts and
  performs repairable follow-up work.
- L5 Diff / TreeDelta is always structural first: path, tree, blob, action, and
  object identity. Optional content-aware inspectors such as JSON-key,
  Markdown-region, or DOCX-part strategies may enrich conflict-policy inputs
  through a registry, but they must not become the source of truth and must not
  block the structural fallback.
- New content-aware diff logic must be added as a strategy under
  `write_engine/tree_delta/content/strategies/` or registered from a product
  composition root. L5 Core must not grow file-extension branches for every
  format. Strategies return bounded machine regions, never full user-facing
  render diffs or large document bodies.
- Conflicts belong to L5. The Write Engine compares base/current/incoming
  trees, reaches a `resolve conflicts` checkpoint, and either produces a new
  candidate tree or writes a pending-conflict fact. L5 Follow-up may surface,
  notify, index, and repair those committed facts, but it must not decide merge
  policy or advance refs.
- L2 is one auth/identity layer with four adjacent resolver partitions. Protocol
  adapters still extract different credential shapes, but all of them resolve
  to the same `AuthContext` contract.
- L3 is the permission layer. Product root, AP/CLI scope, Git remote, and
  connector/job targets apply their own permission checks while sharing the
  same permission vocabulary: target scope, mode, excludes, allowed actions,
  connector status, and audit identity.
- "AP scope auth" means the product concept, not the removed historical
  table model. The canonical runtime model is Project-owned repository state +
  `repository_scopes` + target-bound `access_surfaces`.
- Write side effects are behind `VersionTransactionLedger`. The
  Write Engine decides the lifecycle facts; Supabase persistence lives in
  `version_engine/infrastructure/supabase/transaction_ledger.py`.
- `VersionEngineContainer` is the app/worker bootstrap boundary. Routers depend
  on FastAPI-provided services; workers build an explicit container at
  bootstrap instead of importing hidden singletons.
- L6 is the storage substrate below the write system. ObjectStore backends,
  S3/Supabase physical layout, object-location indexes, bundle/chunk storage,
  hash verification, and storage compatibility shims live here. L6 does not
  perform auth, permission, merge/conflict policy, or ref publication.
- The canonical project root is the only source of truth for new writes. Scoped
  heads and scope-state rows are compatibility/cache material and must be
  rebuildable from the root, not the other way around.
- Git smart HTTP must expose exactly one Git-visible ref state per Access Point
  view. Clone/fetch, receive-pack advertisement, receive quarantine, and push
  fast-forward checks all use the same `GitViewHead` resolver. If current content
  is healthy but old history is damaged, the view is `history_degraded` and Git
  exposes a projected boundary commit. If current content is damaged, the view is
  `current_corrupt` and Git rejects until the current tree is repaired/restored.

Correctness boundaries:

- L1 is intentionally protocol-specific. Do not route the Product UI through
  AP-FS just to share an endpoint; Product root writes, scope access keys, Git
  credentials, MCP keys, and service actors have different request shapes.
- L2 is the single auth/identity decision point. A valid credential gives an actor
  and binding, not write permission.
- L3 is the permission decision point. The authenticated actor must fit inside
  a root/scope target with mode, excludes, connector status, ref policy, and
  audit policy applied.
- L4 is the intent-adapter layer. It converts an already-admitted request into
  `OperationWriteIntent + splice_fn` or `VersionSubmissionIntent`. Syntactic
  cleanup, content serialization, default messages, and Git pack parsing are
  adapter-local implementation details, not a separate architecture layer.
- L5 is the write convergence zone. No route, connector, CLI handler, Git adapter,
  worker, or MCP tool may publish refs, history, audit, conflicts, or outbox rows
  outside the Write Engine.
- L6 owns object bytes and object-location persistence as a substrate for L5.
  Callers above L5 must not treat L6 as a write-authority bypass.
- Conflict decisions must be made in L5. Read surfaces may display conflicts,
  and async jobs may notify or repair conflict views, but they must not decide
  merge policy or advance refs.
- The `resolve conflicts` checkpoint may use policy-driven last-write-wins,
  agent-assisted merge, or manual human resolution. The architecture diagram
  intentionally treats these as strategies behind one checkpoint.
- L5 Follow-up / Repair is the final write-system follow-up area. It may lag
  briefly, but every derived view must be repairable from committed version
  facts. Read APIs and frontend screens are consumers outside the write pipeline.
- The root-first invariant is a correctness boundary, not a performance
  optimization. If a scope cache, Git view cache, path index, or projection is
  empty or stale, reads must repair it from the canonical root or fail loud; they
  must not translate derived failure into an empty project.

## L5 Scope And Branch Convergence

L5 is the only place where different product views, Access Point views, and
Git-visible heads converge into one project history. A scope may look like a
small repository to a user, but it is not an independent source of truth.

```text
Canonical project state
=======================

  root head R10
  root tree T10
      |
      +-- docs/                 subtree D10
      +-- product/              subtree P10
      +-- New Folder (2)/       subtree N10

Derived Git-visible scope state
===============================

  /docs scope head S10
      tree(S10) == D10

  /New Folder (2) scope head G10
      tree(G10) == N10

Invariant
=========

  root is authority.
  scope heads are view heads.
  tree(scope head) must match subtree(root tree, scope_path)
  when the view is healthy.
```

The word "branch" in this section means a user-visible write lane or Git-visible
view lane. Puppyone scope remotes currently expose a single normal Git branch
for the view, while L5 keeps the semantic merge point at the project root.

具体的根写 / 子 scope 写 / 并发处理流程见后面"嵌套 Scope 拓扑"与"并发场景与
冲突解决"两节；这里只保留性能与修复契约。

### Performance Shape

Root-first does not require flattening the whole project for every scoped write.
The normal scoped write path works on Git tree hashes:

- extract the current subtree hash at `scope_path`;
- validate changed relative paths against the admitted scope and excludes;
- replace that subtree hash under the root by rebuilding only the ancestor path;
- publish the new root hash with CAS.

In tree terms, replacing `/docs/api` rebuilds `api`, then `docs`, then root. It
does not read and rewrite every file in unrelated root directories.

Follow-up derived work is also bounded by changed paths:

- only scopes intersecting the committed changed paths are candidates;
- each affected scope is refreshed at most once for the commit;
- scope-state rows and Git view caches are materialized views;
- caches may be rebuilt from committed root facts and object storage.

Current implementation note: some child-scope follow-up merge paths materialize
files inside the affected scope to preserve independent child edits. That work is
limited to the affected scope, not the full project. If a single scope becomes
very large, the intended optimization is to replace that flatten/merge step with
tree-diff and path-patch operations behind the same L5 contract.

### Failure And Repair Contract

L5 Core must make the committed root facts durable before returning success. L5
Follow-up / Repair may lag or retry, but it cannot be the only place where the
project truth exists.

If a follow-up step fails:

- the canonical root remains correct;
- source scope heads from the accepted transaction remain owned by that
  transaction;
- affected non-source scope caches may be stale;
- reads may repair or synthesize a Git-visible scope view from the root;
- durable outbox/repair jobs may rebuild scope-state, Git view caches, path
  indexes, and search indexes;
- stale follow-up jobs must use CAS and must not overwrite newer scope heads.

A broken derived view must never become an empty project. If the root is healthy
and a derived cache is missing, stale, or damaged, the system should rebuild from
the root or fail loudly with a repairable error.

The rebuild implementation remains a derived L5 follow-up operation. Git
clients reach it through the machine `/git/.../rebuild-cache` entry point after
RuntimeGrant admission; human Web operators reach the same implementation
through the Project control-plane adapter after ProjectGrant authorization.
The adapter boundary must not make JWT a valid Git transport credential or
create another publication path.

## Rules

1. Git owns version facts: objects, trees, commits, refs, clone/fetch/push.
2. PuppyOne owns collaboration policy: scopes, auth, conflict handling, audit,
   projections, and server-side transaction semantics.
3. Frontend and Product API writes always target the root product scope unless
   an explicit access point or connector scope is being used.
4. Access-point and connector scopes are wrappers over the canonical project
   root. A scoped write patches only its admitted path, then publishes a new
   root; it does not create an independent source of truth.
5. Git view caches are protocol caches only. They are not authority. They are
   durable per-view derived resources consumed by L1/L4 Git transport and
   repairable from L5 committed facts by L5 Follow-up / Repair.
   Smart-HTTP fetch is stateless across `info/refs` and `git-upload-pack`, so
   upload-pack may temporarily pin client `want` objects as request-local refs
   when the canonical view advances between those two HTTP requests.
6. Search and indexing consume committed events and views; they never decide
   merge/conflict behavior.
7. L6 storage substrate is a physical persistence boundary, not a product
   policy boundary.
8. Runtime code must not import the old external version package or public old
   wire protocol. Git helpers are PuppyOne-owned.

## Folder Layout

```text
backend/src/version_engine/
  bootstrap/
    container.py                  # app/worker scoped service graph
    dependencies.py               # FastAPI dependency boundary

  domain/
    conflicts.py                  # conflict data contracts
    errors.py                     # domain/application error types
    intents.py                    # write/submission/resolution intents

  admission/
    identity.py                   # L2 JWT/access-key/service identity
    channel_pause.py              # channel-level pause gate
    permission.py                 # L3 root/scope/ref/action permission
    repo_facade.py                # repo-shaped target facts
    target.py                     # TargetAdmission contract
    validation.py                 # path/content/limit validators

  entrypoints/
    http/
      access_point.py             # access-key resolution route
      access_point_fs.py          # Puppyone CLI scoped FS API
      audit.py
      conflict.py
      content.py                  # frontend content router composition
      content_history.py
      content_read.py
      content_write.py
      download_token.py
      schemas.py
      shadow_snapshot.py
      websocket.py
    git/
      auth.py                     # Git credential extraction
      router.py                   # Git smart-HTTP route shell

  adapters/
    product/
      commands.py                 # Product/AP/batch write command helper
      operation_adapter.py        # typed tree-operation adapter
      tree_patch.py               # splice helpers for tree mutations
    git/
      object_quarantine.py
      protocol.py
      receive_pack.py
      submission.py
      upload_pack.py
      view_cache.py
      view_projection.py
    batch/
      in_process_client.py

  write_engine/
    engine.py                     # L5 Core facade / intent orchestration
    audit.py                      # shared audit metadata + write logging
    cas_retry.py                  # CAS-retry convergence merge
    conflict_policy.py
    conflict_queue.py             # pending manual-review conflict persistence
    diff.py                       # compatibility wrapper for legacy dict diffs
    git_commit.py
    git_object_format.py
    hash_utils.py
    ledger.py                     # persistence contract
    merge.py
    path_utils.py
    publisher.py                  # L5 CAS publish boundary + follow-up scheduling
    root_state.py                 # root-first state, repair, and scope grafting
    scope_view.py                 # scoped Git-view commit materialization
    scope.py
    submission_commit.py          # Git submission commit preservation/synthesis
    trace.py
    tree.py
    tree_access.py                # tree lookup and sparse merge helpers
    tree_delta/                   # L5 structural write diff / ChangeSet
      models.py                   # TreeChange / TreeDelta contracts
      builder.py                  # tree/file-map/manifest delta builders
      directory.py                # directory add/delete expansion
      projection.py               # changed paths and history/audit rows
      content/                    # optional content-aware machine diff enrichment
        models.py                 # ContentDelta contract
        registry.py               # deterministic strategy routing + fallback
        strategy.py               # ContentDeltaStrategy protocol
        builtins.py               # default strategy composition root
        strategies/
          json.py                 # JSON key/path machine regions
          text.py                 # text/Markdown line machine regions
          docx.py                 # DOCX zip-package part machine regions
    tree_objects.py

  storage/
    # L6 Storage Substrate
    object_store.py               # L5-facing ObjectStore / StorageBackend boundary
    io_strategy.py                # route logical objects to loose/bundle/chunked IO layouts
    backends/
      s3.py                       # S3/Supabase physical backend and location index

  derived/
    # L5 Follow-up / Repair
    git_transport_cache.py
    hooks.py
    notifications.py
    object_gc.py
    object_gc_worker.py
    outbox.py
    path_index.py
    projection.py

  read/
    admin.py                       # narrow orchestration for legacy/admin reads
    history_cache.py               # bounded LRU/TTL + per-snapshot single-flight
    history_cursor.py              # authenticated immutable-ref paging cursors
    history_facts.py               # canonical head + immutable commit decoding
    history_graph.py               # all-ref DAG traversal and page projection
    history_models.py              # typed History read-model contracts/errors
    history_changes.py             # persisted change normalization
    text_detection.py
    tree_reader.py

  infrastructure/
    s3/
      object_storage.py           # compatibility shim; new code imports storage/backends/s3.py
    supabase/
      __init__.py                 # safe_data helper
      audit_backend.py
      audit_repository.py
      db_names.py                 # isolated persisted DB names
      history_repository.py
      repo_manager.py
      scope_manager.py
      scope_repository.py
      server_repo.py
      transaction_ledger.py       # L5 persistence for ledger.py
```

## Persistent DB Names

Canonical physical names are exposed through
`infrastructure/supabase/db_names.py`:

```text
version_commits
version_scope_state
version_view_commits
version_outbox
version_object_locations
version_conflicts
projects.version_root_hash
github_sync_log.version_commit_id
publish_version_project_update
get_version_project_write_state
get_version_project_history_refs
claim/complete/fail_version_outbox
```

The deployment migration temporarily retains restricted `mut_*` compatibility
views/wrappers and dual-write columns so old and new pods can overlap. Runtime
code never targets them. Product code,
frontend code, CLI code, logs, and API metadata should use Version Engine,
Git Remote, Puppyone CLI, scope, conflict, and audit language.

## Project History Read Model

Cloud project History is a dedicated read model over immutable Git objects; it
is not a second source of truth and does not participate in writes. The first
topological page obtains canonical main plus root-scope branch/tag refs from
`get_version_project_history_refs` in one PostgreSQL MVCC snapshot. The RPC
resolves main against `projects.version_root_hash`, then uses the persisted
project-view index and compatibility rows only as ordered fallbacks.

Pagination is bound to that exact ref snapshot. An authenticated, stateless
cursor carries the ordered root commit IDs, snapshot digest, canonical head,
and exclusive anchor, so continuation requests do not reread mutable refs and
remain valid across API replicas. Ref metadata is returned only on the first
page (`refs_included=true`); clients retain it while appending pages and reject
a different `snapshot_id`.

`read/history_graph.py` owns traversal and deterministic child-before-parent
ordering. It reports unreadable objects as an explicit degraded graph instead
of silently claiming completeness. Traversal has a hard commit/ref budget, and
the app-scoped cache is bounded by TTL, snapshot count, and retained-container
weight with single-flight builds. Each actual build logs root count, commit
count, unreadable count, and elapsed time; those measurements are the gate for
any future persistent graph index. The legacy linear catch-up contract remains
independent of the named-ref control-plane read.

For this commit-graph read model, annotated (including nested) tags are peeled
to their commit target exactly as `git log --all` does. The persisted Git ref
still points at the original tag object; only the History response's
`commit_id` is the peeled target used for traversal and label placement.

## Access Point Model

Each access point behaves externally like a repo endpoint, but internally it is
a scoped facade over the canonical project root:

```text
RepositoryTarget + ResolvedRepositoryView
  -> RepoFacade(project_id, repo_id, scope_path, excludes, mode, ref)
  -> Git transport / CLI FS scoped view
  -> Version Engine transaction
  -> shared project object store + canonical project root
  -> optional scope-state / Git-view cache refresh
```

This keeps the GitHub-like external product model without creating one physical
Git repository per scope and without creating one source of truth per scope.

The public Git locator and credential contract lives in
[Git Remote Locator, Credential, And Access Point Contract](05-git-remote-accesspoint.md).
Its canonical routes are a root Project locator and an exact scoped
locator:

```text
/git/{project_id}.git
/git/{project_id}/scopes/{scope_id}.git
```

The route supplies non-secret target identity and HTTP authorization supplies
the opaque credential. L2 must prove that credential owner, Access Surface,
Scope, Project, lifecycle, current ProjectGrant, and effective mode all match
before emitting a RuntimeGrant. From that grant onward the existing Version Engine contract is
unchanged: RepoFacade, GitViewHead, cache identity, quarantine, scope/exclude
admission, VersionSubmissionIntent, canonical-root CAS, audit, and repair do not
depend on the URL family or raw credential.

In particular, the Git view cache remains keyed by effective content geometry
(`project_id + scope_path + excludes + projection/storage variants`), not by
credential, user, route family, local checkout, or Scope ID. Credential rotation
therefore never creates a new content view or invalidates a transport cache.

## 嵌套 Scope 拓扑 —— 用户行为对照表

为了把"嵌套 scope 下用户能怎么写、我们怎么回应"讲清楚，下面用一个具体的拓扑
做参照，把所有用户可能的提交行为列成对照表。

### 参考拓扑

```text
Root
├── /A         ← Scope A
│   └── /A/C   ← Scope C
└── /B         ← Scope B
```

下文"Root 直辖区"指 root 下面、**不在** `/A` 和 `/B` 任何一个挂载点里的位置
（例如 `/README.md`、`/docs/intro.md`）。

### 一、从 Root 入口提交

入口包括产品 Web 保存、API、Project-root CLI/Git Access Surface。Root 入口能
看见整棵树，触达任意路径都是合法的。

| # | 触达路径 | 我们怎么做 | 为什么 |
|---|---|---|---|
| 1 | 只动 Root 直辖区 | 接受 | 标准根写。 |
| 2 | 只动 A 自己的地盘 | 接受，A 视图刷新 | Root 是父，对子 scope 有完全权限。 |
| 3 | 只动 C 的地盘 | 接受，C 视图刷新 | 同上。 |
| 4 | 只动 B 的地盘 | 接受，B 视图刷新 | 同上。 |
| 5 | Root 直辖区 + A 自己 | 接受 | 一次根写，原子刷新所有受影响视图。 |
| 6 | A 自己 + C | 接受 | A 和 C 视图都按 changed-paths 刷新。 |
| 7 | A + B（跨兄弟） | 接受 | 跨兄弟改动的合法入口只有 Root。 |
| 8 | C + B | 接受 | 同上。 |
| 9 | Root 直辖区 + A + C + B 全开 | 接受 | "大重构"型提交；所有视图都会被刷新。 |

**接受时我们干啥（按顺序）：**

1. 客户端提交的就是整棵 root 的新版本，**新 root 树直接拿来用**（不用 graft，root 入口就是站在 root 上写的）。
2. build 一条新的 root commit `C_canon`，parent = 当前 root 的 head commit。
3. 拿 `(旧 root_hash → 新 root_hash)` 去 CAS `projects.version_root_hash`。撞车就拿新 root 重新 graft + rebuild commit 再 CAS，重试到上限为止。
4. CAS 成功后**同一个数据库事务里**做完下面这些：
   - 写 `version_scope_state[scope_path='']`：`scope_hash = 新 root_hash`、`head_commit_id = C_canon`（这一行是 Project-root 投影视图缓存，不是 Scope 资源）。
   - 写 `version_commits` 一行（canonical commit 记录）。
   - 写 `version_transactions` 一行（事务记录）。
   - 写 `audit_logs` 一行。
   - 写 `version_outbox` 一行 `project_version_committed`（通知用）。
5. 事务提交后，**按 changed_paths 一一处理被波及的子 scope 视图**：对每个声明过的 scope `S`（A、B、C 都要看），判断 `changed_paths` 跟"S 自己看得见的路径集合"（= S 自己的 path 子集 - S 已声明的孙 scope）有没有交集：
   - 没交集 → 这个 scope 视图就不动，head 保持原值。
   - 有交集 → 从新 root 按 `S.path` 派生出 `S` 的新 scope_hash；用这个 scope_hash build 一条**合成 commit**（author = `puppyone-scope-view`，message = `Puppyone scope view for <C_canon>`，parent = 该 scope 上一次的 head）；CAS 写 `version_scope_state[S]` 一行 `(scope_hash, head=合成 commit)`。
6. outbox 投递通知 → 受影响 scope 上挂着的 SSE / connector / Git client 拿到 push 通知，下一次 fetch 就看到新 head。

**几个具体场景的差别**（接 上表行号）：

| 行 | changed_paths 与 A 看得见的相交？ | 与 C？ | 与 B？ | 步骤 5 实际刷哪几个 |
|---|---|---|---|---|
| 1 | 否 | 否 | 否 | 不刷新 |
| 2 | **是** | 否（C 被 A 把 /A/C carved 隐了，根改 /A 自己时 C 看不见） | 否 | 只刷 A |
| 3 | 否（A 看不见 /A/C） | **是** | 否 | 只刷 C |
| 4 | 否 | 否 | **是** | 只刷 B |
| 5 | **是** | 否 | 否 | 只刷 A |
| 6 | **是** | **是** | 否 | 刷 A 和 C |
| 7 | **是** | 否 | **是** | 刷 A 和 B |
| 8 | 否 | **是** | **是** | 刷 C 和 B |
| 9 | **是** | **是** | **是** | A、B、C 都刷 |

**为什么这样**：root 入口写没有"客户端原始 commit SHA 需要保留"的需求（用户从 Web/API/CLI 写时不存在一个跟服务端 commit 对应的 client SHA），所以 root 行和被波及 scope 行都用服务端 build 的 commit。源 scope 是 root 自己，所以"源 scope skip 派生"这条规则在这里等同于 root 行已经在步骤 4 写过、步骤 5 不重复处理 root。

### 二、从 Scope A 入口提交

A 默认看不到 `/A/C/*`（已声明的子 scope 在父视图里自动隐藏）。下面是用户
可能尝试的所有写法。

| # | 触达路径 | 我们怎么做 | 为什么 |
|---|---|---|---|
| 1 | 只动 A 自己的地盘 | 接受 | 标准 scoped 写，graft 回 root，C 子树原样不动。 |
| 2 | 只动 C 的地盘 | 拒绝 | A 视图根本看不见 C；这种 tree 一定是绕开默认视图手动构造的。拒绝信息提示："这些路径属于 Scope C，请用 C 的 access key push。" |
| 3 | A 自己 + C 一起动 | 拒绝整次提交 | 不做"部分接受"，保证一次 push 的原子性。让用户要么拆成两次，要么把 C 那部分拿到 C 入口去 push。 |
| 4 | 动 B 的地盘（跨兄弟） | 拒绝 | 越界，scope 之间没有兄弟权限。 |
| 5 | 动 Root 直辖区（往父跑） | 拒绝 | 越界。要动父就用 root access key 或走产品 API。 |

**拒绝的 4 行（#2~#5）怎么处理**：在 admission（L3）期就判完，请求**不进** Write
Engine，没有任何 git object 落盘、没有 root_hash 改动、没有 version_scope_state 改动。
错误码统一 `out_of_scope`，response 带着违规路径和"请去 Scope X push"的建议。

**接受的 #1（只动 A 自己）我们干啥（按顺序）：**

假设客户端是 Git push，本地已经构造好 commit `C_client`（base = A scope 上一次的 head）。

1. **splice 出 A 的新子树**：把客户端提交的 tree（A 视图下那棵树）作为 A 的新子树；C 子树在 A 视图里是 carved 隐藏的，**原样从老的 root 里钉回来**——也就是说客户端那棵树即使在 A 看不见的位置摸了 `/A/C/*`，我们也不让它进。
2. **graft 回 root**：拿当前 root 的整棵树，把 `/A` 那一格替换成步骤 1 得到的 A 新子树，得到 candidate 新 root 树。
3. build canonical root commit `C_canon`，parent = 当前 root 的 head commit。message trailer 里写 `PuppyOne-Original-Commit: C_client`，方便溯源。
4. CAS `projects.version_root_hash: 旧 → 新`。撞车就拿当时新的 root 重新 graft（步骤 2）+ 重新 build commit（步骤 3）再 CAS。重试期间客户端 SHA `C_client` 一直**不变**——只换 canonical root commit 的 parent。**但 publish 时必须同时带上 A 当前 head 的 expected base；如果 A 的 head 已经从客户端 base 前进了，整次事务失败并返回 `non-fast-forward`，不能继续把 `C_client` 写成 A 的 head。**
5. CAS 成功后同一个事务里：
   - 写 `version_scope_state[scope_path='']`：`scope_hash = 新 root_hash`、`head = C_canon`（root 行用服务端 SHA）。
   - 写 `version_scope_state[scope_path='/A']`：`scope_hash = A 的新子树 hash`、`head = C_client`（**源 scope 行刻意沿用客户端 SHA**，这样 A 上 `git fetch` 是 fast-forward，客户端不需要 REWRITE 协议）。
   - 写 `version_commits` / `version_transactions` / `audit_logs` / `version_outbox` 各一行（同根入口）。
6. 事务提交后看 changed_paths（只在 A 自己的地盘里）：
   - A 自己：源 scope，**显式跳过**，否则会把 A 行的 `C_client` 覆盖成合成 commit，fast-forward 链立刻断。
   - C：A 写不到 /A/C/*（admission 拦了），所以与 C 视图必然不相交 → 不刷。
   - B：跟 A 完全不相交 → 不刷。
   - 结果：**没有任何派生 scope 需要刷新**。
7. outbox → notify。

**最终各 scope 上 `git ls-remote` 看到的 head**：

| Scope | head | 来源 |
|---|---|---|
| Root | `C_canon` | 服务端 build |
| /A | `C_client` | **客户端原 SHA**（fast-forward 友好） |
| /A/C | 不变 | 没刷 |
| /B | 不变 | 没刷 |

**关键不变量**：从 Scope A 入口 Git push，A 上 server-blessed head SHA == client commit SHA。
这是 puppyone 区别于 josh REWRITE 协议的核心选择。

### 三、从 Scope C 入口提交

C 是最里层，没有更深的子 scope。

| # | 触达路径 | 我们怎么做 | 为什么 |
|---|---|---|---|
| 1 | 只动 C 自己的地盘 | 接受 | 标准 scoped 写；graft 回 canonical root，再刷新受影响的 scope cache/read view。 |
| 2 | 动 A 自己的地盘（往父跑） | 拒绝 | 越界。 |
| 3 | C + A 一起动 | 拒绝整次提交 | 同上，不做"部分接受"。 |
| 4 | 动 B / Root 直辖区 | 拒绝 | 彻底越界。 |

**接受的 #1（只动 C 自己）我们干啥（按顺序）：**

跟"二、Scope A 入口"差不多，但要注意 C 是 A 的子 scope，root 入口看 C 这里改了，
**A 的视图也会跟着变**（因为 A 默认 carved 看不见 /A/C 的内容，但根上动了 /A/C 时
要让 A 上看到一个"C 这块变了"的提示——具体策略见步骤 6）。

1. **splice 出 C 的新子树**：客户端提交的 tree（C 视图下）就是 C 的新子树（C 没有更深子 scope，不用 carve）。
2. **graft 回 root**：当前 root 的整棵树，把 `/A/C` 那一格替换成步骤 1 的 C 新子树，得到 candidate 新 root 树。注意这里换的是 root 里 `/A/C` 那一格，不是 `/A`——`/A` 那一格里其它内容（A 自己的文件）原样保留。
3. build canonical root commit `C_canon`，parent = 当前 root head。trailer 写 `PuppyOne-Original-Commit: C_client`。
4. CAS root_hash，撞车 rebase 重试（同 A 入口）。**但 publish 时必须同时带上 C 当前 head 的 expected base；如果 C 的 head 已经从客户端 base 前进了，整次事务失败并返回 `non-fast-forward`。**
5. CAS 成功后同一个事务里：
   - 写 `version_scope_state[scope_path='']`，head = `C_canon`。
   - 写 `version_scope_state[scope_path='/A/C']`，head = `C_client`（**源 scope 用客户端 SHA**）。
   - 写 commits / transactions / audit / outbox 各一行。
6. 事务提交后看 changed_paths（都在 /A/C/* 里）跟各 scope 视图相交情况：
   - C：源 scope，跳过。
   - **A**：默认 carved 把 /A/C 当 hidden，A 视图里 /A/C 是空（或仅一个标记），所以 changed_paths 跟 A 视图实际上**不相交** → **A 不刷新**。
   - B：完全不相交 → 不刷。

   也就是说，**纯粹只动 /A/C/* 的写不会触发 A 的视图刷新**，A 上看到的 head 跟以前一样。这是 carved visibility 的直接结果。
7. outbox → notify。

**最终各 scope 上看到的 head**：

| Scope | head | 来源 |
|---|---|---|
| Root | `C_canon` | 服务端 build |
| /A | 不变 | A carved 看不见 /A/C，没刷 |
| /A/C | `C_client` | 客户端原 SHA |
| /B | 不变 | 无关 |

### 四、从 Scope B 入口提交

B 没有子 scope，和 A、C 互不相交。

| # | 触达路径 | 我们怎么做 | 为什么 |
|---|---|---|---|
| 1 | 只动 B 自己的地盘 | 接受 | 标准 scoped 写。 |
| 2 | 动 A / C / Root 直辖区 | 拒绝 | 越界。 |

**接受的 #1 我们干啥（按顺序）：**

B 是最简单的形态——独立子树，没有子 scope，跟 A、C 都不相交。

1. splice：客户端 tree 就是 B 的新子树（没有子 scope 要 carve）。
2. graft：root 里 `/B` 那一格替换成新 B 子树，得到 candidate 新 root。
3. build canonical commit `C_canon`，parent = 当前 root head，trailer 带 `C_client`。
4. CAS root_hash，撞车 rebase 重试。**但 publish 时必须同时带上 B 当前 head 的 expected base；如果 B 的 head 已经从客户端 base 前进了，整次事务失败并返回 `non-fast-forward`。**
5. 事务里：
   - 写 `version_scope_state['']`，head = `C_canon`。
   - 写 `version_scope_state['/B']`，head = `C_client`。
   - commits / transactions / audit / outbox。
6. changed_paths 都在 /B/* 里：B 是源 scope 跳过；A、C 完全不相交 → **没有任何派生 scope 需要刷新**。
7. notify。

**最终各 scope head**：Root = `C_canon`，B = `C_client`，A / C 不变。

### 特殊行为

下面这几类不属于"普通改动文件"，单列出来。

| 场景 | 我们怎么做 |
|---|---|
| 从 Root 入口删掉整棵 `/A/C` 子树 | 接受。Scope C 的 `repository_scopes` 声明保留，C 视图变成空（clone 出来无 ref）。dashboard 弹一条告警："Scope C 的挂载点被根写清空。" C 的所有者可以继续 push 来重建。 |
| 从 Root 入口把 `/A/C` 这个目录改成一个文件 | 走冲突策略。生成 pending conflict，归属 C；由 C 所有者或管理员决定是接受类型变更（C 之后变空）还是回滚。 |
| 跨 scope 边界的 rename / move（如 `/A/x.md` → `/A/C/x.md`） | 子 scope 入口（A 或 C）都做不了：源或目标必有一端越界。这种动作只能从 Root 入口走。 |
| 在已有内容上新声明一个子 scope（如新增 `/A/D` 的 `repository_scopes` 行） | 不搬动任何字节。声明落盘后 A 的下一次 push 触达 `/A/D/*` 会开始被拒；`/A/D` 作为 Scope D 自己可写。 |
| 只读 scope（`mode = r`）上 push | 一律拒绝（403），不区分触达路径。 |
| 子 scope push 与并发根写撞同一文件 | 走 L5 标准冲突策略。冲突行归属 = 路径的声明 scope（如 `/A/C/*` 归属 C）；`audit_detail.actor_source_scope` 留下写入者来源。 |

### 设计原则总结

上面表格里所有行的判断，背后只有两条用户记得住的原则：

1. **从哪个 access point 进，只能动那个 scope 自己看得见的地盘。** 越界的
   push 在 admission 期就被拒掉，不接受"半提交半拒绝"。
2. **要一次跨多个 scope 改，只能从 Root 入口走。** 这是 root 作为唯一真理
   来源最直接的体现。

### 实现侧契约

| 关注点 | 落点 |
|---|---|
| 计算 `carved_excludes_for(scope_path)`（把已声明的后代 scope 自动当作隐藏路径） | `admission/permission.py`、`admission/repo_facade.py` |
| 把 carved excludes 注入 `TargetAdmission.scope_excludes` | `admission/target.py` |
| admission 期拒绝越界路径并返回友好信息 | `admission/validation.py` |
| graft 时把被隐藏的子 scope 子树原样钉回 | `write_engine/scope_view.py`、`write_engine/merge.py` |
| 子 scope 写入后刷新祖先/后代可见性缓存 | `derived/projection.py`、`derived/hooks.py` |
| 根写时按 changed-paths 刷新受影响后代 scope 视图 | `derived/projection.py`、`derived/hooks.py` |
| 挂载点被清空时的 dashboard 告警 + view health | `derived/hooks.py`、`adapters/git/view_projection.py` |
| 冲突行归属 = 路径的声明 scope | `write_engine/conflict_queue.py`、`write_engine/conflict_policy.py` |

### 并发场景与冲突解决

PuppyOne 的并发处理建立在两条公理上：

1. **真理串行化**：Project root_hash 用 CAS 保护，任意时刻只有一个写赢，其它写要么自动重试要么明确失败。
2. **源 scope head 也要 CAS**：写源 scope 行时必须带上"客户端以为的 base"，与当前 head 不匹配就整事务失败。这条**必须有**——只 CAS root_hash 不够，下面有完整推导。

带着这两条公理，下面把所有可能的并发情况挨条过一遍：谁先谁后、能不能自动合并、用户体感是什么。

#### 一、同一源 scope 上的并发 push（关键情况）

**例子**：Alice 和 Bob 都从 `H0` 出发，改 `/A/readme.md` 的不同行，几乎同时 push 到 scope A。

**怎么解决**：

- 第一个 push（设是 Alice）走完 splice + graft + root CAS + publish，scope A 的 head 从 `H0` 走到 `C_alice`。
- 第二个 push（Bob）在 publish 时发现"我以为 base 是 `H0`，但服务端 A 现在 head 已经是 `C_alice`" → **整事务回滚**，回给 Bob 一个 `non-fast-forward` 错误。
- Bob 收到错误后按 Git 习惯 `git pull --rebase`，把自己的改动重新建在 `C_alice` 之上变成 `C_bob'`（parent = `C_alice`），再 push，这次通过。

**为什么不让 Bob 那次自动合并**：Bob 本地的 `C_bob` 这个 commit 对象，其 tree 只含 Bob 改的那行——客户端是基于 `H0` build 的，根本不知道 Alice 的改动。如果服务端在 root 层把 Alice + Bob 自动合并、却还把 scope A 的 head 写成 Bob 的客户端 SHA，那 A 上 `head_commit_id` 这个 commit 指的 tree（Bob-only）就**不等于** `scope_hash`（合并版）——head 指针和 scope_hash 不是同一棵树。后果是别人在 A 上 `git pull` 时，要么 Alice 的改动凭空消失，要么看到非 fast-forward 报错。这种状态不能进入数据库。

**关键不变量**（必须始终成立）：

> 任何一行 `version_scope_state[scope].head_commit_id` 所指 commit 的 tree，必须等于该行的 `scope_hash`。

这条不变量从根上禁止了"head 指针和 scope_hash 是两棵不同 tree"的状态。任何未来代码改动只要违反它就 publish 失败。

**同一行也被同时改怎么办**：与不同行场景**走完全相同的路径**——第二个直接 `non-fast-forward`，让 Bob 自己 `git pull --rebase` 时再处理 textual conflict。**不要**把这种情况引到 hosted conflict review 里去——hosted conflict 是为跨 scope / 跨入口的真正产品级冲突准备的，同源 scope 的并发是 Git 协议层的事，混在一起两个修复路径都模糊。

#### 二、不同 scope 之间的并发 push

**例子**：Alice push 到 scope A 改 `/A/x`，Carol 同时 push 到 scope B 改 `/B/y`。

**怎么解决**：

- 谁先 root CAS 成功就先落地。
- 输的那个 CAS retry：拿新的 root 作为底，把自己的子树 graft 回去——因为路径完全不相交，graft 没有冲突，build 出新的 canonical root commit 重新 CAS。
- retry 时**源 scope 那行的 expected base 不变**——A push 看的是 A 上次的 head，B push 看的是 B 上次的 head——两个 scope 的 head 互不影响，两次 publish 的源 scope CAS 都会过。
- 用户体感：两次都成功，互相不感知。

#### 三、子 scope push 与根写撞同一路径

**例子**：Alice 从 scope A 入口 push 改 `/A/readme.md`，与此同时管理员从 Root 入口 PAPI 写也改了 `/A/readme.md`。

**怎么解决**：

- 谁先 root CAS 成功就先落地——设根写先到，root_hash 走到 `R_1`。
- Alice 的 CAS 失败 retry：把新 root `R_1` 作为底重新 graft。此时**路径相交检测发现 `/A/readme.md` 在双方都改了**。
- 这是真正意义上的"两个入口对同一文件的语义冲突" → **进 hosted conflict 路径**，写一行 pending_conflict 归属 scope A，等人决断。
- 用户体感：Alice 收到 `pending review`（不是 `non-fast-forward`），通过产品 UI 看冲突详情。

**与情况一的关键区别**：情况一是**同一入口**两人改动，按 Git 协议本就该让客户端 rebase 解决；情况三是**两个不同入口**（一个 Git 客户端、一个 Web 编辑器/PAPI）对同一文件的并发，客户端层面没办法协调，必须由产品介入。两条路径不混用。

#### 四、子 scope push 与根写不撞路径

**例子**：Alice 从 scope C 入口 push 改 `/A/C/y`，根写改 `/B/z`。

**怎么解决**：

- 谁先到谁先赢 root CAS。
- 输的那个 retry：路径不相交，自动 merge 完成，新 canonical commit build 出来。
- 源 scope 行的 CAS：对 C 的写，expected 是 C 上次的 head——根写没动 C，所以 C 的 head 没变 → CAS 通过；对根写，expected 是根上次的 head——刚被前一次写动过，CAS 失败 → 它再 retry 一次，从更新后的 root 出发，再 publish，最终通过。
- 用户体感：两次都成功，互相不感知。

#### 五、连续两个根写（自动 merge 域）

**例子**：管理员通过 PAPI 同时改 `/A/x` 和 `/B/y`，前后两次根写。

**怎么解决**：

- 第一次 CAS 成功落地。
- 第二次 CAS 失败 → retry：从新 root 出发，重新合并，重新 CAS 直到通过或达到重试上限。
- 因为根写是服务端构造的 canonical commit，每次 retry **服务端自己重新 build** 新的 commit object，head 指针和 tree 自然永远一致，不变量天然满足。
- 用户体感：两次都成功，可能稍慢（retry 的开销）。

#### 六、非 Git 入口（PAPI / CLI / connector）的并发

非 Git 入口没有客户端原始 commit SHA。这些入口的源 scope CAS expected = "intent 进 L4 时读到的当前 scope head"。

- 并发场景下 CAS 失败 = 服务端内部 retry：重读 base + 重走 splice + graft + 重新 build commit + 重新 CAS。这个 retry 对**调用方完全透明**——PAPI 客户端只看到一次返回。
- 因为这条路径的源 scope commit 也是服务端 build 的 canonical commit，head 指针与 scope_hash 永远一致，不变量天然满足。
- 唯一例外：retry 期间发生了**真正的产品语义冲突**（同一文件被两边改），那进情况三的 hosted conflict 路径。

#### 七、Git push 与非 Git 写撞同一 scope

**例子**：Alice 从 scope A push，与此同时 connector 把 ingest 进来的文件也想写进 scope A。

**怎么解决**：

- 谁先 root CAS 成功就先落地。
- Git push 那次的 CAS 用 Alice 的 `H0` 作为 expected——connector 已经写过的话，A 的 head 已经动了 → Alice 拿 `non-fast-forward`。
- Connector 那次的 CAS 用调度时读的 head 作为 expected——Alice 已经写过的话，connector 在内部 retry，重读 head、重 splice、再 CAS。
- 用户体感：Git 用户被要求 rebase，connector 用户透明 retry。

#### 八、Force push

Git 原生 `git push --force` 允许非 fast-forward。puppyone 默认禁用，要开启需要在 scope 上 opt-in 一个权限位。

- opt-in 后那一次 push 的源 scope CAS expected 用一个 sentinel（如 `*`）跳过相等校验。
- **但 head 与 tree 一致性的不变量继续必须满足**——force push 只解除"必须 fast-forward"这一条，不解除"head 指针和 scope_hash 必须一致"。
- 用户体感：A 的 head 可以跳到任意 commit，但不能跳到一个 tree 与 scope_hash 不符的 commit。

#### 九、首次 push（scope 还没 head）

新声明的 scope 第一次被 push，`version_scope_state` 里压根没这行。

- 源 scope CAS 的 expected = 空（或 null）。
- publish 的 upsert 走 INSERT 分支——没有现存行可以比对，自动通过。
- 用户体感：和正常 push 一样成功。

#### 十、Derived sync 与后续直接 push 的竞争（次级 race）

这是**次级 race**，目前实现里也存在，但影响轻于情况一。

**例子**：根写 1 落地，post-commit hook 排队准备刷新 A 的视图；与此同时 A 自己来了一个 Git push 落地。

**怎么解决**：

- post-commit hook 计算 A 的 target_scope_hash 是基于**触发它的那个 root_hash**（旧）；而 A 上的直接 push 已经基于**新 root_hash** 写过 A 行了。
- hook 的 CAS 不能盲目用"自己计算时刻"的派生结果去覆盖 A 当前的 head——必须带上 `root_hash` 版本号：如果数据库里 root_hash 已经超过 hook 触发时的版本，hook 应放弃这次同步，让后续更新的那次 root_write 的 hook 来收尾。
- 用户体感：A 的 head 永远反映**最新一次写**，不会被过期 hook 抹掉。

这条修复优先级低于情况一，但思路一致：**任何写源 scope 行的动作都要带版本，过期写不能覆盖**。

#### 速查：每种并发的最终归宿

| 并发类型 | 落到哪条路径 |
|---|---|
| 同源 scope 改不同文件（Git） | 第二个 `non-fast-forward`，client rebase |
| 同源 scope 改同一文件（Git） | 第二个 `non-fast-forward`，client rebase（**不进 hosted conflict**） |
| 不同 scope（Git/Git） | 自动 merge，都成功 |
| 子 scope vs 根写 撞路径 | hosted conflict，pending review |
| 子 scope vs 根写 不撞路径 | 自动 merge，都成功 |
| 连续根写 | 自动 merge，都成功 |
| 非 Git 入口之间 | 内部 retry，调用方透明 |
| Git vs 非 Git 撞同 scope | Git 那次 `non-fast-forward`；非 Git 那次内部 retry |
| Force push（opt-in） | 跳过 fast-forward 校验，仍守 head/tree 一致性 |
| 首次 push | 通过 |
| Derived sync vs 后续 push | 带 root 版本号的 CAS，老 hook 让位 |

#### 紧扣的不变量

整套并发处理底下只有两条不变量：

1. **写源 scope 行必须带客户端 base（Git）或当时读到的 head（非 Git），CAS 不过整事务回滚。**
2. **`version_scope_state[scope].head_commit_id` 所指 commit 的 tree 必须等于该行的 `scope_hash`。**

任何并发场景的正确性都从这两条推出来。**修复落地前，运维上对 Git 客户端用户文档加一条提示：并发 push 到同一 scope 可能出现 view drift；产品端短期把单 scope 写串行化（节流到 1 QPS/scope）。修复落地后这条提示可以撤掉。**

---

未来如果想在某个 scope 上 opt-in "透明可见"（子 scope 的内容能在父 view
里看见、父 push 能写子的地盘），可以加一个
`repository_scopes.nested_visibility = transparent` 设置。Transparent 模式下，
"父入口写子地盘"要走对子 scope head 的 base-version 校验；该开关存在
之前，所有嵌套 scope 都按上面表格里的方式处理。
