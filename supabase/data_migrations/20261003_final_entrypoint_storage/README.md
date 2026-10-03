# Final entrypoint storage — ISSUE-049

This is the sole D05/D06 data artifact. It does not guess a row's owner and does
not prove that deployed clients/producers have exited. Keep B1 and the earlier
20260927 artifact unchanged. Its Contract must precede this Contract.

## Release A

1. Deploy only `20261003220000_expand_final_entrypoint_storage.sql` with the
   compatible application. Source-only tables/decision infrastructure are
   additive; the old records remain authoritative until cutover.
2. Complete and verify `20260927_entrypoint_storage_backfill` before freezing
   its Access/search tables. Keep its exact checksum and receipt.
3. Inventory/stop all old producers, reconcile/drain queues and durable work,
   and capture a consistent restore point while the relevant writers are
   stopped. A flag or an empty Redis scan is not evidence of stopping them.
4. Use `scripts/entrypoint_source_decisions.py inventory --file <new-private-file>`.
   It exports IDs, redacted facts and row+run fingerprints, with no chosen
   dispositions or credential/config values. Review every row using real caller
   and history evidence. The input has format_version=1 and rows; each row needs
   disposition, explicit Import source ID where relevant, reviewer and evidence.
5. `approve --file <reviewed-file> --apply` records only those reviewed choices.
   Missing, stale, duplicate, foreign or conflicting decisions are rejected.
   Import-only keeps its original ID and cannot discard any durable binding/run
   fact. Dual-use needs an explicit source ID/relationship; an unsupported
   historical binding additionally needs an explicit read-only retention reason.
   A reviewed binding-only historical record may be retained read-only WITHOUT
   fabricating an Import source, including its opaque original configuration.
   The final application must suppress that private configuration in metadata
   and reject execution/reactivation; this is not an executable compatibility path.
6. `freeze --file <reviewed-consumer-evidence> --restore-point-ref <reference>
   --approved-by <reviewer> --apply` records the evidence digest and freezes
   application writes. Evidence must name the environment and actual records,
   and attest producer_stop_verified, queue_drain_verified and old_consumers_exited.
   This command does NOT independently verify those external statements.
7. Run/verify `20261003_final_entrypoint_storage` through `puppyone-db`. The
   runner owns the transaction and receipt; do not insert a receipt manually.
   It copies exact encrypted config/timestamps/identity but removes no old row.

All commands use DATA_MIGRATION_DATABASE_URL via libpq environment, not argv.
The decision tool defaults to loopback and requires explicit remote opt-in plus
SUPABASE_PROJECT_ID/SUPABASE_URL pairing for hosted targets. Those switches are
not a substitute for authorization. Inventory files are new, mode 0600 files.

## Release B

After the actual Release A and consumer gates pass, separately promote the
20260927 and this artifact's `contract.pending.sql` byte-for-byte into active
migrations, in that order, and deploy the matching final-schema application.
Do not roll an all-at-once legacy database push past a missing data receipt.
The final Contract locks, rechecks fingerprints/copies/receipts, separates dual
config, removes only verified Import-only rows, physically renames tables and
columns, preserves run/history/credentials, updates Activity/SQL/constraints,
revokes direct user grants and removes all private transition tables/functions.

A fresh installation with no legacy/user footprint can apply the final Contract
without an irrelevant old-data receipt. Populated installations cannot use that
exception. Final release pointers select this artifact; an older verifier that
names removed physical tables is not the final-release verifier.

## Recovery

Do not resume old application writes after verified copying and then blindly
reuse that completion receipt: the frozen fingerprints would no longer match.
Before Contract, either finish the coordinated cutover or restore the reviewed
pre-data state with compatible old binaries and repeat review. A full database
restore requires quiescence of ALL covered writers, not just these tables.
After Contract, old binaries are not a rollback target. Use a final-schema
compatible roll-forward build or a matched database/object-store restore point
that includes all accepted post-cutover writes. Rehearse this; never discard
new writes just to make an old schema runnable.

`scripts/test_final_entrypoint_storage.py` exercises real PostgreSQL DDL,
permissions, missing/stale/lossy/colliding reviews, atomic rollback/receipt,
exact-copy preservation, fresh/populated Contract, post-contract retry and
pg_dump/restore retaining accepted post-contract writes and receipts.
Its native mode stubs Supabase Auth infrastructure and is not the separate full
Supabase/GoTrue/Provider/worker/installer or hosted deployment acceptance.
