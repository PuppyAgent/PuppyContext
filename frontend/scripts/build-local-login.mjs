import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { cp } from 'node:fs/promises';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
// Explicit build; Desktop must never implicitly run this expensive operation.
// Keep it separate from both a live .next HMR cache and deploy verification.
const child = spawn(process.execPath, [
  '--max-old-space-size=2048',
  path.join(root, 'node_modules/next/dist/bin/next'),
  'build',
], {
  cwd: root,
  stdio: 'inherit',
  detached: process.platform !== 'win32',
  env: { ...process.env, PUPPYONE_NEXT_DIST_DIR: '.next-desktop-login', PUPPYONE_LOCAL_BUILD: '1' },
});
let stopping = false;
async function stopBuild(signal) {
  if (stopping || !child.pid) return;
  stopping = true;
  if (process.platform === 'win32') {
    const killer = spawn('taskkill.exe', ['/PID', String(child.pid), '/T', '/F'], { stdio: 'ignore', windowsHide: true });
    killer.once('error', () => child.kill(signal));
    return;
  }
  const alive = () => {
    try { process.kill(-child.pid, 0); return true; }
    catch (error) { if (error.code === 'ESRCH') return false; throw error; }
  };
  if (!alive()) return;
  process.kill(-child.pid, signal);
  const deadline = Date.now() + 5_000;
  while (alive() && Date.now() < deadline) await delay(25);
  if (alive()) process.kill(-child.pid, 'SIGKILL');
}
for (const signal of ['SIGINT', 'SIGTERM']) {
  process.once(signal, () => void stopBuild(signal).catch(error => {
    console.error('Local preview build cleanup failed:', error);
    process.exitCode = 1;
  }));
}
child.once('error', error => { console.error(error); process.exitCode = 1; });
child.once('exit', async (code, signal) => {
  process.exitCode = code ?? (signal ? 1 : 0);
  if (code !== 0 || stopping) return;
  try {
    // Next standalone intentionally omits these assets. Match Dockerfile.local
    // so the built login works in a browser, not just as an HTML health probe.
    const dist = path.join(root, '.next-desktop-login');
    await cp(path.join(root, 'public'), path.join(dist, 'standalone', 'public'), { recursive: true });
    await cp(path.join(dist, 'static'), path.join(dist, 'standalone', '.next-desktop-login', 'static'), { recursive: true });
  } catch (error) {
    console.error('Local preview asset preparation failed:', error);
    process.exitCode = 1;
  }
});
