# Formal native repository data migration

The user approved implementation on 2026-10-07, restricted to local Docker
validation. Hosted reads are optional; hosted writes, deployment and migration
execution are outside this change's execution authorization.

Add an immutable operator data artifact and additive SQL control plane. Convert
legacy Git/MUT storage into canonical Git objects, preserve source records,
record object mappings, and activate checked native repositories. Reuse the
existing data runner and operator-local release verification. Public CI tests
only disposable local Supabase/S3 services and needs no hosted secrets.

Old Scope configuration and credentials are retained without upgrading their
permissions. Native Scope implementation, automatic source deletion and removal
of old runtime consumers are separate changes. A successful data migration is
not evidence that every product entrypoint supports native repositories.
