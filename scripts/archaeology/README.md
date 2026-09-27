# Schema Archaeology Tools

Two long-lived tools for keeping `supabase/migrations/*.sql` and live databases in agreement.

Both tools are kept after the 2026-04-18 alignment effort (see [`docs/archaeology/ALIGNMENT_COMPLETE.md`](../../docs/archaeology/ALIGNMENT_COMPLETE.md)) because they make schema drift detectable and fixable for the future.

## `dig.sh` — periodic schema audit

**What it does:** Dumps the live schema of `qubits` and `prod`, builds the canonical `expected` schema by applying every file in `supabase/migrations/` to a fresh local Postgres, then three-way diffs the results.

**When to run:**
- Monthly health-check (or quarterly).
- After any large migration sequence lands on prod.
- When CI's drift detector starts warning.
- When you suspect someone made an out-of-band change in the SQL Editor.

**Setup (one-time on each Mac):**

```bash
brew install libpq postgresql@17
```

**Run:**

```bash
export QUBITS_DB_PASSWORD='...'   # from Supabase dashboard → qubits → Settings → Database
export PROD_DB_PASSWORD='...'     # from Supabase dashboard → prod → Settings → Database
./scripts/archaeology/dig.sh
```

**Output:** `docs/archaeology/<TIMESTAMP>/` (gitignored — your local diagnostic, not a repo artifact). Open `<TIMESTAMP>/README.md` for the headline numbers, then read the three `*.diff` files for details.

**Reading the result:**

| Diff | Meaning if non-empty |
|---|---|
| `qubits-vs-prod.diff` | Two live envs out of sync — usually because one got a manual SQL Editor change |
| `qubits-vs-expected.diff` | Qubits drifted from the migration files — manual change happened, or a migration didn't apply cleanly |
| `prod-vs-expected.diff` | Prod drifted from the migration files — same root cause, more dangerous |
| `applied-migrations-diff.txt` | The two `schema_migrations` tables disagree about which migrations were applied — investigate immediately |

Cosmetic noise to ignore: `\restrict`/`\unrestrict` lines (random session ids), `Dumped from database version` lines, column physical-position swaps with no other delta.

## `test-align-migration.sh` — local validation for drift-cleanup migrations

**What it does:** Spins a transient local Postgres, applies migrations in three scenarios, and asserts that schema invariants hold:

1. Fresh DB + every migration in order — must succeed
2. Fresh DB + early migrations + injected drift state + the drift-cleanup migration — must succeed
3. Re-running the drift-cleanup migration on already-aligned DB — must be a no-op (idempotency)

**When to run:** Before opening a PR for any migration that fixes drift (i.e. operates on data already in prod, like the `align_legacy_drift` / `soften_access_points_user_fk` pair from 2026-04-18). Skip for plain "add a column" migrations — those are easier to validate via `supabase db reset`.

**Setup:** Same as `dig.sh` — needs `postgresql@17` from Homebrew.

**Run:**

```bash
./scripts/archaeology/test-align-migration.sh
```

Adapt the script's `inject_drift` function and `verify_schema` assertions to whatever new drift-cleanup migration you're testing. The current contents are tuned for the 2026-04-18 alignment migrations; treat them as a template.

## Why these two and not the throw-away scripts?

The 2026-04-18 archaeology produced ~10 one-shot scripts (`apply-alignment.sh`, `diagnose-null-orgs-deep.sh`, etc.). All of them did their job — they brought qubits and prod into agreement with the migration files — and were deleted afterward. The two scripts here are the only ones that retain value because they are **re-runnable diagnostic tools**, not one-time fixes.

If you ever need to look at how the alignment was actually done, see:

- [`supabase/archive/before_b1/migrations/20260418040000_align_legacy_drift.sql`](../../supabase/archive/before_b1/migrations/20260418040000_align_legacy_drift.sql) — the bulk reconciliation
- [`supabase/archive/before_b1/migrations/20260418050000_soften_access_points_user_fk.sql`](../../supabase/archive/before_b1/migrations/20260418050000_soften_access_points_user_fk.sql) — the FK softening
- [`docs/archaeology/ALIGNMENT_COMPLETE.md`](../../docs/archaeology/ALIGNMENT_COMPLETE.md) — full narrative

The deleted scripts can also be recovered from git history if needed.
