# Direct native object transport

Git objects remain immutable canonical bytes behind StorageBackend. Refs and
HEAD remain byte-valued PG facts. Each reader retains an admitted snapshot/pin;
client OIDs are readable only when reachable from admitted published roots.
Object bodies use a bounded request cache, never an in-memory full repository.

Wire framing and negotiation are separate from object traversal and pack
encoding. A mature format codec is an adapter dependency, not another version
authority. Push pack/delta expansion is bounded before allocation, staged
privately, hash/type/closure checked, and published only through the existing
RefTransactionService. Uncertain publication still requires result recovery.

Fetch emits compressed objects incrementally with backpressure and retains the
read pin until completion/cancellation. Scratch files may buffer incoming bytes
and newly uploaded objects, with existing request/object budgets. No existing
repository graph is written to scratch, and no native transport invokes git.
Admission stays bounded: removing per-request repository copies does not claim
one process can sustain 1000 CPU/network-heavy transfers simultaneously.

The existing durable closure verifier remains conservative; optimizing its
cross-request proofs is separate from removing filesystem materialization.

Validation compares stock Git clients and object/ref identities, including both
formats and the existing workflow matrix. New tests reject any server-side Git
process/bare init, exercise cold reads, incremental requests, malformed packs,
concurrent readers/writers, cancellation and memory/disk budgets. No deployment
or capacity claim is made solely from component tests.
