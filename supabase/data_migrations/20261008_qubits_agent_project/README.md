# Qubits Agent project rollout

This immutable operator artifact selects **one** acceptance project in `run.py`
and `verify.sql`. It does not certify the remaining historical inventory. A
future selection needs its own immutable artifact and receipt. The converter
primitives are copied from the previous immutable artifact into the single
entrypoint, so their bytes are covered by this artifact's checksum.

Normal CI uses synthetic fixtures. Ordinary deployment verifies schema and the
selected project's receipt; it does not read all hosted projects or scan S3.
Historical repair and global archival/Contract remain independent operator work.
Do not promote the destructive recovery Contract as part of this release.

After the compatible schema is installed and relevant writers/GC are drained:

```sh
NATIVE_MIGRATION_WRITERS_DRAINED=yes uv run puppyone-db run 20261008_qubits_agent_project
uv run puppyone-db verify 20261008_qubits_agent_project
```

Supply the protected, matching Qubits database and object-store environment from
the manifest. The portable runner owns locking, checksum and completion receipt.
The selected project's full Git history is verified, copied and retained using
the existing converter; no history is truncated. Missing selected source bytes
still fail explicitly. Source retention remains enabled.

The one-time organization usage baseline measures **current trees only** through
the existing reconciliation protocol. A sibling's historical graph is never
read or migrated. Snapshot drift, missing current data, policy violations or
incomplete measurements reject activation atomically. This baseline is migration
work, not a startup, chat, or normal CI/CD step. Repeating a completed selection
does not repeat its baseline capture.

Activation changes only selected projects. Unselected roots, refs, objects and
history remain intact. They are not claimed to be native-ready; the native-only
runtime continues to return explicit readiness errors for unmigrated projects.
Completing this selected rollout does not justify a whole-environment readiness
claim. Subsequent healthy-project migration and historical repair use separate
bounded selections, rather than blocking this acceptance on the entire database.

Synthetic Docker PostgreSQL/S3 tests cover a selected two-commit repository next
to an unreadable sibling history, preservation of that sibling, idempotence,
current-tree drift rollback and recovery. Hosted acceptance additionally checks
actual Agent read/edit replies and per-run database/timing metrics.
