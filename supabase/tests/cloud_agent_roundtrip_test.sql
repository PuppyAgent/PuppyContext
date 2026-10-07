SELECT plan(1);
\ir _support/cloud_agent_roundtrip.inc
SELECT pass('Agent submission, retry, claim, publication fence and durable reply work on the staged schema');
SELECT * FROM finish();
