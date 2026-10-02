# Git Remote Locator, Credential, and Access Contract

This document is the normative contract for PuppyOne Git hosting and Desktop
Cloud discovery. It separates three concerns that must never be collapsed:

1. a canonical URL locates a repository target;
2. a current human session authorizes Cloud UI actions;
3. an independent Git credential authorizes smart-HTTP data-plane actions.

The Cloud does not register, attest, inventory, or identify a local device,
folder, checkout, or Desktop workspace instance.

## Resource model

```text
Organization                         ownership, membership, billing
└── Project                          canonical authorization boundary
    ├── Canonical Git Repository     one object database, commit graph, refs
    │   ├── Project-root view        target { kind: project_root, project_id }
    │   └── Scoped views             target { kind: scope, project_id, scope_id }
    ├── ProjectGrant                 current human control-plane authority
    └── Access Surfaces
        └── Git Surface
            └── Git credentials      independent, hash-only runtime principals
```

A Scope is path geometry and an authorization restriction inside the canonical
Project repository. It is not a second repository and does not own a separate
object database.

## Canonical locators

```text
Project root: /git/{project_id}.git
Scope view:   /git/{project_id}/scopes/{scope_id}.git
```

The URL contains only non-secret target identity. Credentials are supplied by
Git HTTP authentication and must never appear in a newly generated URL.

Desktop accepts a canonical remote as an automatic local-to-Cloud association
only when all of these hold:

- exactly one PuppyOne canonical target is present;
- its origin matches the configured Cloud Git origin;
- the URL parses to a normal Project-root or Scope target;
- the current JWT authorizes that Project and exact target.

No canonical remote means `local-only` for the local association resolver. That
resolver makes no repository-context request and renders no association error.
This does not prohibit account-level Cloud navigation: a signed-in Desktop user
may list accessible Projects using the existing `GET /api/v1/projects/` and
explicitly open one after exact-target authorization and file preflight. An
untrusted or malformed local remote must not block that independent catalog.
A legacy secret-bearing route may still be served by the Git transport during
its bounded compatibility window, but it is never a Cloud UI locator.

## Cloud UI resolution

Automatic association for an open local repository:

```text
User asks for the local repository's Cloud association
  -> Desktop reads actual Git remotes
  -> no unique canonical PuppyOne remote?
       -> local-only; stop; no Cloud request
  -> Desktop parses { project_id, optional scope_id }
  -> POST /api/v1/projects/{project_id}/repository-context
       body: { target }
       auth: current human JWT
  -> Backend checks path Project ID == target Project ID
  -> AuthorizationService resolves current ProjectGrant
  -> Scope target is loaded and checked inside the same Project
  -> return secret-free Project context and capabilities
  -> render Project content
```

Independent account navigation:

```text
Current configured Cloud connection + authenticated human session
  -> GET /api/v1/projects/ (existing authorization-filtered catalog)
  -> explicit Project selection (not a local-folder binding)
  -> POST /api/v1/projects/{project_id}/repository-context { target }
  -> authorize exact Project / optional Scope, as above
  -> read the authorized file root
  -> open the hosted workspace; leave local files and Git remotes unchanged
```

Both paths use the same existing Project and authorization contracts. Empty
Project creation remains `POST /api/v1/projects/` with an explicit `org_id` and
`Idempotency-Key`; it does not publish local content. This Desktop navigation
change requires no database, schema, migration, or server-side binding change.
Desktop owns its navigation lifecycle; its canonical contract is
`puppy-issues:document/puppyone-desktop/cloud/workspace-state.md`.

The backend receives no local path, workspace ID, device ID, checkout ID, Git
credential, or raw remote URL on either path. The canonical URL is a locator,
not proof. A cached ID cannot override Git association or current authorization;
user-selected hosted navigation is separate from claiming a local association.

## Git data-plane authorization

```text
git fetch / push
  -> canonical route resolves requested target
  -> HTTP credential is hashed before lookup
  -> credential resolves owner + exact Git Access Surface + target + mode
  -> current membership/role is re-evaluated
  -> effective mode = min(
       credential ceiling,
       Surface policy,
       Scope max_mode,
       current ProjectGrant
     )
  -> RuntimeGrant enters Git transport
```

The route target and credential target must match exactly before repository
metadata or object state is opened. A RuntimeGrant cannot enter Team, Billing,
membership, sharing, Project settings, or credential-management APIs.

## User Git credentials

An authenticated user whose current `ProjectGrant` authorizes the requested
mode may issue a credential for one exact target through:

```text
POST /api/v1/projects/{project_id}/git-credentials
DELETE /api/v1/projects/{project_id}/git-credentials/{credential_id}
```

The authenticated client generates the plaintext credential with a CSPRNG.
Desktop puts it in operating-system protected storage; the web UI holds it only
in memory for the one-time display. The POST request carries that value once so
the backend can store its keyed hash. Its response contains only credential ID,
mode, and canonical remote locator; the backend never generates or echoes the
plaintext. Storage contains only a keyed hash, prefix/last-four hints, owner
user ID, Project, Access Surface, target mode, status, and timestamps. It
contains no local-client identity.

Issuance requires a canonical UUIDv4 `Idempotency-Key`. The operation record is
keyed by authenticated user and operation key. A same-payload retry resolves to
the original credential ID; a changed payload is rejected. Thus a retried
publish operation has at most one effective credential.

The generic Access Surface `regenerate-key` route cannot issue human Git
credentials and returns `410 Gone` for a Git Surface. This prevents a caller
from bypassing client-generated, user-owned, idempotent issuance.

Multiple clients may hold independent credentials for the same user and target.
Revoking one credential does not revoke the others. Removing a local remote is
a local Git operation and does not imply credential revocation. A transient
failure after credential issuance remains in Electron main's durable publish
journal and resumes with the same operation and vault-backed secret. Only an
explicit Abandon request may revoke the operation credential, and only after
the server proves that the same operation still owns an unpublished, empty
Project.

Membership loss invalidates user credentials on their next request. A role
downgrade dynamically caps an existing read-write credential to read-only;
credential rows are not rewritten merely because a role changed.

### Publish-operation credential

First-time Desktop publication uses the same credential model with a narrower
recovery contract. Electron main generates the secret, protects it in an
OS-backed vault, and supplies an operation UUID as `Idempotency-Key`. The
service persists only the keyed hash and redacted hints. Same operation key and
same canonical target/mode/secret hash replay the credential identity; reuse
with a different payload fails closed.

This operation key makes response-loss recovery deterministic. It is not a
client, checkout, device, or folder identity. After publication the resulting
credential remains an ordinary independently revocable user Git credential.
Abandon may revoke it only while the same operation's Project is still empty.

## Local state

Git config is authoritative for Cloud discovery:

```text
remote "puppyone" -> canonical locator
credential helper -> plaintext secret under operating-system protection
```

The shared workspace config may store sync, backup, branch, and preferred remote
settings. It must not store Cloud authorization, a server-issued checkout ID,
or a secret. `workspaceInstanceId` is a local application concern only and
never crosses the Cloud API boundary.

## Failure semantics

- no canonical remote: local-only, no error;
- more than one canonical target: local conflict with repair guidance;
- wrong host: reject before sending a Cloud request;
- 401: sign-in/session recovery;
- 403: current account lacks Project access;
- 404: Project or Scope no longer exists;
- transport outage: temporary-unavailable state, preserving local Git state;
- session generation change: retry internally; never expose `SESSION_CHANGED`.

## Non-negotiable invariants

- No server table or API represents an installed computer or local checkout.
- No Cloud UI authorization is derived from a Git credential or URL alone.
- No Git RuntimeGrant is derived from a human JWT alone.
- No Project-root repository is represented as a synthetic Scope.
- No raw credential is persisted in application data or returned by list/read.
- No credential issue response contains raw credential material.
- No raw publish credential is persisted by Cloud or in a plaintext Desktop
  journal; it stays behind Electron main's OS-backed secret-vault boundary.
- No arbitrary or legacy Git remote identifies Cloud UI context.
