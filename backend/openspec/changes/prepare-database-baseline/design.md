# Baseline preparation and adoption boundary

The existing `supabase/migrations` chain remains the schema source of truth.
A candidate is derived from a fresh replay, not from a live database dump or
concatenation of historical scripts. PostgreSQL's own schema dump preserves
owners, grants, defaults, policies, constraints and function definitions. The
application's trigger on the platform-owned Auth table is captured separately.
Required data is reviewed explicitly and compared with the fresh replay.

Only an owned temporary workdir is reset. The tool has no remote target option.
Docker hosts the CLI-managed real Supabase stack. Database commands run through
the owned container, avoiding ambient PG credentials. CI needs no cloud secrets.

Preparation yields `supabase/baselines/b1/{baseline.sql,manifest.json,...}`.
This is a candidate staging location; the Supabase CLI never applies it. A later
adoption release moves the executable SQL into `migrations/` and removes this
candidate copy. No independently edited snapshot is introduced.

Activation cannot be implemented merely by deleting historical files:
`policy.py` pins the old history, `historical_release.py` refers to old files,
the data runner requires exact schema IDs, and release preflights reference old
objects. All must understand a reviewed baseline transition before activation.
Old databases must complete the public upgrade path and postcondition checks
before any history reconciliation. Data-job receipts remain factual; schema
squashing must not manufacture external storage inventory completion.

PuppyPay currently has five Alembic migrations. It remains independently
versioned; Cloud baseline generation must not capture any private Pay tables.
