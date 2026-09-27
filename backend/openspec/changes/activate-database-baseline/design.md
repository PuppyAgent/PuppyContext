# B1 archive and adoption

`supabase/migrations/20260926000000_baseline_b1.sql` is the sole baseline SQL.
Its 102 sources remain unchanged in `supabase/archive/before_b1/migrations/`.
The manifest maps exact checksums to the archived sources and executable SQL.
The archive is never scanned by normal Supabase deployment.

Fresh databases use ordinary Supabase migration commands. Existing databases
must have the complete archived history and match the reviewed schema, including
permissions, RLS and Auth triggers. Adoption stores the original history rows
in a migration-log receipt and replaces only those history rows in one
transaction. No business data, schema or existing data-job receipts are changed.
Incomplete/unknown histories and schema drift fail before history writes.

Public tooling can materialize the archived chain into an isolated workdir for
older installations and regression tests. Historical application-data phases
retain their existing gates. Applying a baseline does not claim that data jobs
have run; surviving jobs receive schema prerequisite coverage, while obsolete
jobs are rejected with a retired reason rather than run against removed tables.

Hosted deployment performs admission before ordinary db push. Direct Supabase
integration encountering old history fails safely until the transition has run;
do not bypass this with include-all or unconditional repair. Production adoption
remains a staged release through the existing environment gates.

All eight data-task directories are archived beside the schema chain, with
byte-level inventories and unchanged catalog checksums pinned in the manifest.
One catalog serves CLI, releases and operator verification. Whole-release FIFO
queues prevent interleaved phases; data-only changes also require staging evidence.
Manual test inputs use environment variables, dependencies and actions are pinned,
and secret scanning blocks new findings while scheduled scans cover all history.
