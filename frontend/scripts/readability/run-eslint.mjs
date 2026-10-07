import { existsSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('../../../', import.meta.url));
const inputs = ['frontend', 'packages'].filter(path => existsSync(`${root}/${path}`));
const result = spawnSync(process.execPath, [
  'frontend/node_modules/eslint/bin/eslint.js',
  '--config', 'frontend/eslint.config.mjs',
  '--suppressions-location', 'frontend/eslint-suppressions.json',
  ...inputs, ...process.argv.slice(2),
], { cwd: root, stdio: 'inherit' });
if (result.error) throw result.error;
process.exitCode = result.status ?? 1;
