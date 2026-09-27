# Archived schema migrations

`before_b1/` preserves the 102 original Cloud migration files byte-for-byte.
Their SHA-256 checksums and cutoff are pinned in `../baselines/b1/manifest.json`.

Normal Supabase deployment executes `../migrations/`, never this directory.
The public upgrade and regression tools may explicitly stage this history in
an isolated workdir. Do not modify archive files, copy them alongside B1 in the
active directory, or execute the entire archive against a B1 database.

The legacy PostgreSQL 15 Compose bootstrap still mounts its original initial
SQL from here to preserve existing behavior. Current B1 installs require the
Supabase PostgreSQL 17 path; upgrading that legacy stack is a separate release.
