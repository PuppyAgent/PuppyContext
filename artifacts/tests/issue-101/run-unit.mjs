// Keep both Vite config loading and test caches out of shared node_modules.
import { fileURLToPath } from 'node:url';
import { startVitest } from '../../../frontend/node_modules/vitest/dist/node.js';

process.chdir(fileURLToPath(new URL('../../../frontend', import.meta.url)));
const context = await startVitest('test', process.argv.slice(2), {
  root: fileURLToPath(new URL('../../../frontend', import.meta.url)),
  run: true,
  configLoader: 'runner',
  cache: false,
  maxWorkers: 2,
  update: process.env.UPDATE_BASELINE === '1',
}, {
  cacheDir: fileURLToPath(new URL('./.vite', import.meta.url)),
});
if (!context) process.exitCode = 1;
else await context.close();
