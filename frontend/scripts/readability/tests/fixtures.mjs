import { mkdtempSync, mkdirSync, readFileSync, rmSync, symlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

const root = fileURLToPath(new URL('../../../', import.meta.url));

export function write(rootPath, path, value) {
  mkdirSync(dirname(join(rootPath, path)), { recursive: true });
  writeFileSync(join(rootPath, path), typeof value === 'string' ? value : `${JSON.stringify(value, null, 2)}\n`);
}

export function fixture(t) {
  const path = mkdtempSync(join(tmpdir(), 'puppyone-readability-'));
  t.after(() => rmSync(path, { recursive: true, force: true }));
  mkdirSync(join(path, 'frontend'));
  // Read-only access to this worktree's tools; fixtures never install dependencies.
  symlinkSync(join(root, 'node_modules'), join(path, 'frontend/node_modules'), 'dir');
  for (const file of [
    'eslint.config.mjs', '.dependency-cruiser.cjs', 'tsconfig.dependencies.json',
    'scripts/readability/run-eslint.mjs', 'scripts/readability/run-dependencies.mjs',
    'scripts/readability/check-baselines.mjs', 'scripts/readability/baselines.mjs',
  ]) write(path, `frontend/${file}`, readFileSync(join(root, file), 'utf8'));
  write(path, 'frontend/tsconfig.json', { compilerOptions: { baseUrl: '.', jsx: 'preserve' } });
  write(path, 'frontend/eslint-suppressions.json', {});
  write(path, 'frontend/.dependency-cruiser-known-violations.json', []);
  return path;
}

export function command(path, executable, args) {
  const result = spawnSync(executable, args, { cwd: path, encoding: 'utf8', maxBuffer: 10 * 1024 * 1024 });
  if (result.error) throw result.error;
  return { ...result, output: `${result.stdout}\n${result.stderr}` };
}

export function gate(path, script, ...args) {
  return command(path, process.execPath, [`frontend/scripts/readability/${script}.mjs`, ...args]);
}
