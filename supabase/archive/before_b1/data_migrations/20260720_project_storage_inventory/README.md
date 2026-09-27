# Project storage inventory

This immutable migration performs the one-time, resumable inventory required
before the Project deletion admission fence can open. It scans only the legacy
`users/{principal}/(etl_artifacts|processed|raw)/{project}/` namespaces,
records live Project principals, removes only strictly parsed prefixes for
Projects that no longer exist, then requires two matching full S3 listings.

The workflow must receive the same S3 credentials as the Qubits API service.
It is intentionally run through `puppyone-db`, not from a developer machine,
so the Supabase database target and S3 target are bound to the protected
environment and a completion receipt is published.
