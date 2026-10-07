import { spawn } from 'node:child_process';
import { mkdtemp, writeFile, rm, access, cp, symlink, mkdir, readFile } from 'node:fs/promises';
import { constants } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { setTimeout as delay } from 'node:timers/promises';
import { analyzeHeap } from './server-memory-analysis.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const argumentsMap = new Map(process.argv.slice(2).map(arg => {
  const match = /^--([a-z-]+)=(.+)$/.exec(arg);
  if (!match) throw new Error(`Use --name=value, got ${arg}`);
  return [match[1], match[2]];
}));
const allowed = new Set(['mode', 'scenario', 'samples', 'interval-ms', 'requests-per-sample', 'max-growth-mb', 'heap-mb', 'rss-mb', 'timeout-ms', 'cpu-profile', 'snapshot']);
for (const key of argumentsMap.keys()) if (!allowed.has(key)) throw new Error(`Unknown option: ${key}`);
function integer(name, fallback, min, max) {
  const value = Number(argumentsMap.get(name) ?? fallback);
  if (!Number.isInteger(value) || value < min || value > max) throw new Error(`Invalid ${name}: ${value}`);
  return value;
}
const mode = argumentsMap.get('mode') ?? 'preview';
const scenario = argumentsMap.get('scenario') ?? 'health';
if (!['dev', 'preview'].includes(mode) || !['idle', 'health', 'login'].includes(scenario)) throw new Error('Invalid mode/scenario.');
const samplesCount = integer('samples', 30, 6, 10_000);
const intervalMs = integer('interval-ms', 1000, 0, 60_000);
const requestsPerSample = integer('requests-per-sample', 5, 1, 100);
const maxGrowthMb = integer('max-growth-mb', 32, 1, 1024);
const heapMb = integer('heap-mb', 1536, 256, 4096);
const rssMb = integer('rss-mb', 2048, 256, 8192);
const timeoutMs = integer('timeout-ms', 180_000, 10_000, 3_600_000);
for (const key of ['cpu-profile', 'snapshot']) {
  if (argumentsMap.has(key) && !['true', 'false'].includes(argumentsMap.get(key))) throw new Error(`Invalid ${key}`);
}
// Avoid loading real workstation .env files or reporting user data. Run in a
// clean worktree with synthetic public credentials; never reuse a live server.
for (const file of ['.env', '.env.local', `.env.${mode === 'dev' ? 'development' : 'production'}`, `.env.${mode === 'dev' ? 'development' : 'production'}.local`]) {
  try { await access(path.join(root, file), constants.F_OK); }
  catch { continue; }
  throw new Error(`Memory regression refuses ${file}; use a clean worktree and synthetic credentials.`);
}
if (mode === 'preview') await access(path.join(root, '.next-desktop-login', 'BUILD_ID'));
const artifacts = await mkdtemp(path.join(tmpdir(), 'puppyone-web-memory-'));
const testHome = path.join(artifacts, 'home');
await mkdir(testHome);
const nextVersion = JSON.parse(await readFile(path.join(root, 'node_modules', 'next', 'package.json'), 'utf8')).version;
const systemEnvironment = Object.fromEntries(['PATH', 'SystemRoot', 'WINDIR', 'TEMP', 'TMP', 'TMPDIR']
  .filter(key => process.env[key] !== undefined).map(key => [key, process.env[key]]));
// Next dev rewrites tsconfig/next-env when choosing a distDir. Keep all such
// writes (and HMR caches) in a disposable source sandbox, not the task tree.
const workerRoot = mode === 'dev' ? path.join(artifacts, 'sandbox') : root;
if (mode === 'dev') {
  await cp(root, workerRoot, { recursive: true, filter: source => {
    const relative = path.relative(root, source);
    const first = relative.split(path.sep)[0];
    return !['node_modules', 'tests'].includes(first) && !first.startsWith('.next')
      && !first.startsWith('.env') && !relative.endsWith('.tsbuildinfo');
  } });
  await symlink(path.join(root, 'node_modules'), path.join(workerRoot, 'node_modules'), process.platform === 'win32' ? 'junction' : 'dir');
}
const distDir = mode === 'preview' ? '.next-desktop-login' : '.next';
const execArgs = ['--expose-gc', `--max-old-space-size=${heapMb}`];
if (argumentsMap.get('cpu-profile') === 'true') execArgs.push('--cpu-prof', `--cpu-prof-dir=${artifacts}`);
const child = spawn(process.execPath, [...execArgs, path.join(root, 'scripts/server-memory-worker.mjs')], {
  cwd: workerRoot,
  detached: process.platform !== 'win32',
  stdio: ['ignore', 'pipe', 'pipe', 'ipc'],
  env: {
    ...systemEnvironment,
    HOME: testHome,
    USERPROFILE: testHome,
    NODE_OPTIONS: '', // Do not inherit secrets, preload hooks, or user SDK state.
    NODE_ENV: mode === 'dev' ? 'development' : 'production',
    NEXT_TELEMETRY_DISABLED: '1',
    PUPPYONE_NEXT_DIST_DIR: distDir,
    PUPPYONE_MEMORY_MODE: mode,
    PUPPYONE_MEMORY_ARTIFACTS: artifacts,
    NEXT_PUBLIC_SUPABASE_URL: 'http://127.0.0.1:9',
    SUPABASE_URL: 'http://127.0.0.1:9',
    SUPABASE_SERVER_URL: 'http://127.0.0.1:9',
    NEXT_PUBLIC_SUPABASE_ANON_KEY: 'synthetic-memory-test-key',
    SUPABASE_ANON_KEY: 'synthetic-memory-test-key',
    NEXT_PUBLIC_API_URL: 'http://127.0.0.1:9',
    BACKEND_URL: 'http://127.0.0.1:9',
  },
});
let output = '', failure, ready, finished = false, nextId = 0;
let resolveReady, rejectReady;
const readyPromise = new Promise((resolve, reject) => { resolveReady = resolve; rejectReady = reject; });
// Attach immediately: early worker failures must not become unhandled rejections.
readyPromise.catch(() => {});
const pending = new Map();
const abort = new AbortController();
const samples = [];
const usage = [];
const fail = error => {
  failure ??= error;
  rejectReady(error);
  abort.abort(error);
  for (const entry of pending.values()) { clearTimeout(entry.timer); entry.reject(error); }
  pending.clear();
};
child.stdout.on('data', chunk => { output = (output + chunk).slice(-128_000); });
child.stderr.on('data', chunk => { output = (output + chunk).slice(-128_000); });
child.on('error', fail);
child.on('exit', (code, signal) => {
  finished = true;
  if (code !== 0 || !ready) fail(new Error(`Worker exited: ${signal ?? code}`));
});
child.on('message', message => {
  if (message.type === 'ready') { ready = message; resolveReady(message); }
  else if (message.type === 'usage') {
    usage.push(message);
    if (message.rss > rssMb * 2 ** 20) fail(new Error(`RSS budget exceeded (${rssMb} MiB)`));
  } else if (message.type === 'fatal') fail(new Error(message.message));
  else if (pending.has(message.id)) {
    const entry = pending.get(message.id);
    pending.delete(message.id);
    clearTimeout(entry.timer);
    entry.resolve(message);
  }
});
const hardDeadline = setTimeout(() => fail(new Error('Memory test deadline exceeded')), timeoutMs);
function rpc(type, timeout = 15_000) {
  if (failure) return Promise.reject(failure);
  const id = ++nextId;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`${type} timed out`)); }, timeout);
    pending.set(id, { resolve, reject, timer });
    child.send({ type, id }, error => { if (error) { clearTimeout(timer); pending.delete(id); reject(error); } });
  });
}
async function request(origin, pathname) {
  if (failure) throw failure;
  const response = await fetch(new URL(pathname, origin), {
    signal: AbortSignal.any([abort.signal, AbortSignal.timeout(10_000)]), cache: 'no-store',
  });
  const body = await response.text();
  if (!response.ok) throw new Error(`HTTP ${response.status} for ${pathname}`);
  if (pathname === '/api/health') {
    const payload = JSON.parse(body);
    if (payload.service !== 'puppyone-cloud-web' || payload.status !== 'ok' || payload.schemaVersion !== 1 || payload.mode !== (mode === 'preview' ? 'production' : 'development')) throw new Error('Health identity mismatch');
    if (response.headers.has('set-cookie')) throw new Error('Liveness unexpectedly set cookies');
  } else if (!body.includes('Puppyone') || !body.includes('Sign in')) throw new Error('Login page markers missing');
}
async function stop() {
  if (!finished && child.connected) child.send({ type: 'stop' });
  const deadline = Date.now() + 5_000;
  while (!finished && Date.now() < deadline) await delay(25);
  // The custom worker owns no detached descendants; still clear its group on
  // POSIX, including after the wrapper exits. Never signal a live user server.
  if (process.platform !== 'win32') {
    try { process.kill(-child.pid, 'SIGKILL'); } catch (error) { if (error.code !== 'ESRCH') throw error; }
  } else if (!finished) child.kill('SIGKILL');
  if (!finished) await Promise.race([new Promise(resolve => child.once('exit', resolve)), delay(2_000)]);
  if (!finished) throw new Error('Memory worker did not exit');
}
for (const signal of ['SIGINT', 'SIGTERM']) process.once(signal, () => fail(new Error(`Interrupted: ${signal}`)));
let analysis;
try {
  const { port } = await readyPromise;
  const origin = `http://127.0.0.1:${port}`;
  const login = '/login?client=desktop&desktop_state=' + 'a'.repeat(43);
  // Compile/warm both paths before measuring; startup allocations are not leaks.
  for (let i = 0; i < 5; i++) { await request(origin, '/api/health'); await request(origin, login); }
  await rpc('sample');
  for (let i = 0; i < samplesCount; i++) {
    if (failure) throw failure;
    if (scenario !== 'idle') for (let j = 0; j < requestsPerSample; j++) await request(origin, scenario === 'health' ? '/api/health' : login);
    await delay(intervalMs, undefined, { signal: abort.signal });
    samples.push(await rpc('sample'));
  }
  analysis = analyzeHeap(samples, { maxGrowthBytes: maxGrowthMb * 2 ** 20 });
  if (!analysis.passed) throw new Error(`Post-GC retained heap grew ${(analysis.growthBytes / 2 ** 20).toFixed(1)} MiB`);
} catch (error) {
  failure ??= error;
} finally {
  clearTimeout(hardDeadline);
  if (argumentsMap.get('snapshot') === 'true' && child.connected && !finished) {
    // Explicit opt-in: heap snapshots may themselves need significant memory.
    const savedFailure = failure; failure = undefined;
    try { await rpc('snapshot', 30_000); } catch (error) { output += `\nSnapshot failed: ${error}`; }
    failure = savedFailure;
  }
  try { await stop(); } catch (error) { failure ??= error; }
  for (const entry of pending.values()) clearTimeout(entry.timer);
  const report = { mode, scenario, nodeVersion: process.version, nextVersion, samplesCount, intervalMs, requestsPerSample, heapMb, rssMb,
    passed: !failure, error: failure?.message, analysis, samples, usage,
    limitation: 'Finite synthetic test; does not establish absence of a slower leak or production readiness.' };
  await writeFile(path.join(artifacts, 'report.json'), JSON.stringify(report, null, 2));
  await writeFile(path.join(artifacts, 'server.log'), output);
  if (mode === 'dev') await rm(workerRoot, { recursive: true, force: true });
  console.log(JSON.stringify({ passed: !failure, mode, scenario, analysis, artifacts }, null, 2));
  if (failure) { console.error(failure.message); process.exitCode = 1; }
}
