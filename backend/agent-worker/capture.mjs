/** Trusted provider control operation; never exposed as an Agent tool. */
import { readdir, readFile, lstat } from 'node:fs/promises';
import path from 'node:path';
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
let size = 0;
const files = {};
async function walk(directory) {
  for (const entry of await readdir(directory, {withFileTypes: true})) {
    const name = path.join(directory, entry.name);
    const stat = await lstat(name);
    if (stat.isDirectory()) await walk(name);
    else {
      if (!stat.isFile() || stat.isSymbolicLink()) throw new Error('Unsupported recovery file');
      size += stat.size;
      if (size > 64 * 1024 * 1024 || Object.keys(files).length >= 10000) throw new Error('Recovery byte limit exceeded');
      files[path.relative('/workspace', name)] = (await readFile(name)).toString('base64');
    }
  }
}
await walk('/workspace');
process.stdout.write(JSON.stringify(files));
