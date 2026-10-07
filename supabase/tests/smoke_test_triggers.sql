-- pgTAP adapter for the portable post-deploy schema contracts.
--
-- The assertion body lives below _support so pg_prove mounts it without
-- discovering it as a standalone TAP test. Hosted staging/production execute
-- that same body with plain psql and no pgTAP dependency.

SELECT plan(2);
\ir _support/schema_contracts.inc
SELECT pass('portable schema smoke contracts completed without an exception');
\ir _support/cloud_agent_contracts.inc
SELECT pass('Cloud Agent control plane and publication dependencies are confined');
SELECT * FROM finish();
