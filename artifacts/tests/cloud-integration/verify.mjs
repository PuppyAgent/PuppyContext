// Reproduce the final local acceptance checks without installing dependencies.
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { writeFileSync } from 'node:fs';

const root = fileURLToPath(new URL('../../../', import.meta.url));
const output = fileURLToPath(new URL('./', import.meta.url));
const base = 'aac9aca02ef83d0ea807e179126dc1a1675ee944';
const checks = [
  ['lint-prune', ['run', 'lint:prune']],
  ['dependencies-prune', ['run', 'dependencies:prune']],
  ['baseline-ratchet', ['run', 'lint:baselines', '--', base]],
  ['lint', ['run', 'lint']],
  ['dependencies', ['run', 'lint:dependencies']],
  ['unit', ['run', 'test:unit']],
  ['build-ci-final', ['run', 'build']],
];
const results = [];
for (const [name, args] of checks) {
  const env = { ...process.env };
  if (name === 'build-ci-final') {
    delete env.PUPPYONE_LOCAL_BUILD;
    delete env.PUPPYONE_NEXT_DIST_DIR;
    delete env.API_INTERNAL_URL;
    Object.assign(env, {
      NEXT_PUBLIC_SUPABASE_URL: 'https://placeholder.supabase.co',
      NEXT_PUBLIC_SUPABASE_ANON_KEY: 'placeholder-anon-key-for-build-only',
      NEXT_PUBLIC_API_URL: 'http://localhost:9090',
    });
  }
  const result = spawnSync('npm', args, { cwd: `${root}frontend`, env, encoding: 'utf8', maxBuffer: 20 * 1024 * 1024 });
  if (result.error) throw result.error;
  const log = `${result.stdout}${result.stderr}`.replace(/\r/g, '').replace(/[\t ]+$/gm, '').trimEnd() + '\n';
  writeFileSync(`${output}${name}.log`, log);
  const record = { name, command: `npm ${args.join(' ')}`, exit_code: result.status };
  results.push(record);
  console.log(JSON.stringify(record));
  if (result.status !== 0) break;
}
writeFileSync(`${output}final-results.json`, `${JSON.stringify(results, null, 2)}\n`);
process.exitCode = results.some(result => result.exit_code !== 0) ? 1 : 0;
