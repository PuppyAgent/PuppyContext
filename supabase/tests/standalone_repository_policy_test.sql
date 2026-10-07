SELECT plan(1);
\ir _support/standalone_repository_policy.inc
SELECT pass('Standalone native creation works while hosted enforcement and owner-only configuration remain protected');
SELECT * FROM finish();
