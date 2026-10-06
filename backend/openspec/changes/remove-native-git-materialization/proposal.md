# Change: Serve native Git directly from the canonical object store

## Why
Every native fetch/push currently rebuilds a complete reachable bare repository
on server scratch disk. Concurrent requests duplicate already durable objects.
The user explicitly approved removing this mandatory materialization and asked
for implementation, preserving S3 objects and database refs/location indexes.

## What Changes
- Replace native transport's stock-Git subprocess/repository adapter with an
  object-store protocol adapter. Keep the public Git HTTP locator and auth.
- Retain PG ref transactions, current admission, publication durability and GC
  pins. Protocol parsing cannot publish refs directly.
- Stream fetch packs with bounded backpressure; quarantine only incoming push
  data, never copy existing repository objects into a local bare repository.
- Preserve SHA-1/SHA-256, protocol v0/v1/v2, shallow/deepen and partial clones.
- Use a pinned Git format/pack codec dependency only inside the transport
  adapter; PuppyOne's version engine and authority remain owned by PuppyOne.

## Impact
- Native full-project Git transport and its resource/compatibility tests.
- No schema migration, production activation, legacy Scope migration or change
  to authoritative S3 object layout.

Approval: explicit user instruction in the current session to implement the
previously discussed S3 + database object transport without mandatory bare repos.
