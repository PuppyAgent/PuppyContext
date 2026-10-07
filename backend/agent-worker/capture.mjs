/** Trusted provider control operation; never exposed as an Agent tool. */
import { readdir, readFile } from 'node:fs/promises';
const own = process.pid;
for (let attempt = 0; attempt < 8; attempt++) {
  let running = false;
  for (const name of await readdir('/proc')) {
    if (!/^\d+$/.test(name) || Number(name) === own) continue;
    try {
      const status = await readFile(`/proc/${name}/status`, 'utf8');
      if (!/^Uid:\s+1000\s/m.test(status) || /^State:\s+[TZt]\b/m.test(status)) continue;
      process.kill(Number(name), 'SIGSTOP');
      running = true;
    } catch (error) { if (!['ENOENT', 'ESRCH'].includes(error.code)) throw error; }
  }
  if (!running) break;
  if (attempt === 7) throw new Error('Could not quiesce sandbox processes');
  await new Promise(resolve => setTimeout(resolve, 25));
}
const root = '/workspace';
const { captureGit, commitGit, knowledgeFiles } = await import('./git-workspace.mjs');
const state = await captureGit(root);
if (process.argv[2] === '--commit' && state) await commitGit(root, process.argv[3]);
const workspace = await knowledgeFiles(root);
if (process.argv.includes('--workspace')) {
  process.stdout.write(JSON.stringify({ ...workspace, ...(state ? { git: await captureGit(root) } : {}) }));
} else process.stdout.write(JSON.stringify(workspace.files));
