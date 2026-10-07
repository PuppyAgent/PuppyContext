# Puppyone CLI And Git Remote

PuppyOne exposes two supported command-line surfaces.

## Stock Git

Use Git for local working-copy workflows:

```bash
# Project root
git clone https://<host>/git/<project_id>.git ./workspace

# Or one non-root Scope
git clone https://<host>/git/<project_id>/scopes/<scope_id>.git ./workspace

cd workspace
git add .
git commit -m "update context"
git push
git pull
```

The server challenges with HTTP Basic authentication. Use
`x-puppyone-token` as the username and the separately issued, one-time Git
credential as the password. Configure an OS-backed credential helper with
`credential.useHttpPath=true`; never place the credential in the URL, shell
arguments, `.git/config`, or `credential.helper store`.

Git pushes enter the Git smart-HTTP adapter, resolve an exact RuntimeGrant,
validate scope/excludes/mode, and publish through the Version Engine's normal
Git submission path. The root and scoped URL contract is normative in
[Git Remote Locator, Credential, And Access Point Contract](05-git-remote-accesspoint.md).

## Puppyone CLI

Use `puppyone` for control-plane operations and cloud-scoped filesystem actions:

```bash
puppyone ap login root --api-url https://api.puppyone.com
puppyone fs ls
puppyone fs cat notes/readme.md
echo "hello" | puppyone fs write notes/hello.md
puppyone fs rm old.md
```

`puppyone fs` does not clone a full repository. It calls AP-FS routes, which
submit typed product operations through `ProductOperationAdapter`.
Its CLI access key is a separate protocol credential; it is not a Git password
and does not belong in a Git remote URL.

CLI access keys follow the same non-recoverable secret discipline as Git
credentials: storage is hash-only, ordinary Scope/Repo Identity/dashboard
reads return no plaintext, and an authorized user must explicitly generate a
replacement. The create/regenerate response reveals the new `cli_...` value
once. First-party Web UI keeps that value only in the current component state
long enough to copy setup commands; it does not refresh it back from a list,
write it to a URL, or treat a masked hint as an executable credential.

Generating a new CLI key revokes the previous shared CLI credential (including
clients still using the bounded legacy key-in-Git-URL route). It does not rotate
independent Git `r`/`rw` or session credentials.

## Entrypoint control plane (unreleased)

The source CLI separates `import` snapshots, `synchronize` bindings/runs and
`access` Agent/MCP/Sandbox surfaces. Legacy external-source commands must preserve
one-shot versus persistent intent; Access surface IDs are not binding IDs.
See [unreleased commands, compatibility and test gates](../cli/ENTRYPOINTS-UNRELEASED.md).
These source-tree changes do not claim an npm publication or final API cutover.

## Performance Rule

Small Web/API/CLI edits must not materialize a full transport repo or download
unchanged blobs. They use:

- one project write-state RPC,
- tree splices by hash,
- object batch/bundle writes,
- SQL CAS as the publish boundary,
- asynchronous outbox work for search/projection/notifications.
