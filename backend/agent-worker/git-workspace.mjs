/** Git working-copy operations, always inside the provider sandbox. */
import { execFile } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { chmod, lstat, mkdir, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import path from 'node:path';

const maxGit = 32 * 1024 * 1024;
const maxFiles = 64 * 1024 * 1024;

export async function git(root, args, options = {}) {
  const { input, ...rest } = options;
  return new Promise((resolve, reject) => {
    const child = execFile('git', ['-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
    '-c', 'commit.gpgSign=false', '-c', 'maintenance.auto=false', '-c', 'gc.auto=0', ...args], {
    cwd: root, env: { PATH: process.env.PATH, HOME: '/home/node', GIT_CONFIG_NOSYSTEM: '1',
      GIT_CONFIG_GLOBAL: '/dev/null', GIT_TERMINAL_PROMPT: '0', GIT_OPTIONAL_LOCKS: '0' },
    encoding: 'buffer', maxBuffer: maxGit, timeout: 60000, ...rest,
  }, (error, stdout, stderr) => error ? reject(error) : resolve({ stdout, stderr }));
    child.stdin.on('error', error => { if (error.code !== 'EPIPE') reject(error); });
    child.stdin.end(input);
  });
}

export async function knowledgeFiles(root) {
  const files = Object.create(null), modes = Object.create(null);
  let bytes = 0, count = 0;
  async function walk(dir) {
    for (const entry of await readdir(dir, { withFileTypes: true })) {
      if (entry.name.toLowerCase() === '.git') continue;
      const name = path.join(dir, entry.name);
      const stat = await lstat(name);
      if (stat.isSymbolicLink() || (!stat.isFile() && !stat.isDirectory()))
        throw new Error('Unsupported workspace file type');
      if (stat.isDirectory()) await walk(name);
      else {
        bytes += stat.size;
        if (bytes > maxFiles || ++count > 10000) throw new Error('Workspace checkpoint limit exceeded');
        const relative = path.relative(root, name);
        files[relative] = (await readFile(name)).toString('base64');
        modes[relative] = stat.mode & 0o111 ? '100755' : '100644';
      }
    }
  }
  await walk(root);
  return { files, modes };
}

export async function captureGit(root) {
  try {
    const meta = await lstat(path.join(root, '.git'));
    if (!meta.isDirectory() || meta.isSymbolicLink()) throw new Error('Invalid Git working copy');
  } catch (error) { if (error.code === 'ENOENT') return null; throw error; }
  const format = (await git(root, ['rev-parse', '--show-object-format'])).stdout.toString().trim();
  let tip = null, head = null;
  try { tip = (await git(root, ['rev-parse', '--verify', 'HEAD'])).stdout.toString().trim(); }
  catch (error) { if (error.code !== 128) throw error; }
  try {
    const name = (await git(root, ['symbolic-ref', '-q', 'HEAD'])).stdout;
    head = name.subarray(0, -1).toString('base64');
  } catch (error) { if (error.code !== 1) throw error; }
  const refs = (await git(root, ['for-each-ref', '--format=%(objectname) %(refname)'])).stdout;
  // Include staged blobs as well as commits, even when the index and working
  // tree differ. A pack made only with bundle --all would lose staged objects.
  const pack = (await git(root, ['pack-objects', '--stdout', '--revs', '--all', '--reflog', '--indexed-objects'])).stdout;
  const data = Buffer.concat([Buffer.from(`# v3 git bundle\n@object-format=${format}\n`),
    refs, Buffer.from(tip ? `${tip} HEAD\n\n` : '\n'), pack]);
  if (data.length > maxGit) throw new Error('Git bundle exceeds checkpoint limit');
  const bundle = data.toString('base64');
  let index = null;
  try {
    const value = await readFile(path.join(root, '.git/index'));
    if (value.length > 4 * 1024 * 1024) throw new Error('Git index exceeds checkpoint limit');
    index = value.toString('base64');
  } catch (error) { if (error.code !== 'ENOENT') throw error; }
  return { bundle, object_format: format, head, tip, index };
}

export async function restoreGit(root, state) {
  if (!['sha1', 'sha256'].includes(state.object_format)) throw new Error('Unsupported Git format');
  await mkdir(root, { recursive: true });
  const bundle = `/tmp/puppyone-${randomUUID()}.bundle`;
  try {
    if (state.bundle) {
      const data = Buffer.from(state.bundle, 'base64');
      if (data.length > maxGit) throw new Error('Git bundle exceeds checkpoint limit');
      await writeFile(bundle, data, { mode: 0o600 });
      // A real clone retains original objects/history. No checkout can run a
      // repository's filters before the controlled working tree is restored.
      const end = data.indexOf('\n\n');
      if (end < 0) throw new Error('Invalid Git bundle');
      const hasRefs = data.subarray(0, end).toString('latin1').split('\n').some(line => /^[0-9a-f]+ /.test(line));
      if (hasRefs) {
        await git(root, ['clone', '--no-checkout', '--', bundle, root]);
        await git(root, ['fetch', '--update-head-ok', bundle, '+refs/*:refs/*']);
      } else {
        await git(root, ['init', `--object-format=${state.object_format}`, root]);
        await git(root, ['index-pack', '--stdin'], { input: data.subarray(end + 2) });
      }
    } else {
      await git(root, ['init', `--object-format=${state.object_format}`, root]);
    }
    if ((await git(root, ['rev-parse', '--show-object-format'])).stdout.toString().trim() !== state.object_format)
      throw new Error('Cloned repository object format mismatch');
    const head = state.head && Buffer.from(state.head, 'base64').toString('utf8');
    if (head && (!head.startsWith('refs/heads/') || Buffer.from(head).toString('base64') !== state.head))
      throw new Error('Invalid selected Git branch');
    if (head) {
      await git(root, ['symbolic-ref', 'HEAD', head]);
      if (state.tip) await git(root, ['update-ref', head, state.tip]);
      else await git(root, ['update-ref', '-d', head]);
    } else if (state.tip) await git(root, ['update-ref', '--no-deref', 'HEAD', state.tip]);
    else throw new Error('Missing Git HEAD');
    if (state.tip) await git(root, ['read-tree', state.tip]);
    if (state.index) await writeFile(path.join(root, '.git/index'), Buffer.from(state.index, 'base64'));
    await git(root, ['config', 'user.name', 'Puppyone Agent']);
    await git(root, ['config', 'user.email', 'agent@puppyone.invalid']);
    // Cloud publication is a supervisor operation. A bundle is read-only and
    // its temporary pathname must not survive as a misleading writable remote.
    try { await git(root, ['config', '--remove-section', 'remote.origin']); }
    catch (error) { if (error.code !== 128) throw error; }
  } finally { await rm(bundle, { force: true }); }
}

export async function commitGit(root, message) {
  const state = await captureGit(root);
  if (!state) return;
  await git(root, ['add', '-A', '--', '.']);
  try { await git(root, ['diff', '--cached', '--quiet', '--exit-code']); }
  catch (error) {
    if (error.code !== 1) throw error;
    await git(root, ['commit', '--no-gpg-sign', '-m', message]);
  }
}

export async function restoreFiles(root, files, modes = {}) {
  for (const [name, value] of Object.entries(files)) {
    const target = path.resolve(root, name);
    if (!target.startsWith(root + '/') || name.split('/').some(p => ['..', '.', '.git'].includes(p.toLowerCase())))
      throw new Error('Invalid checkpoint path');
    await mkdir(path.dirname(target), { recursive: true });
    await writeFile(target, Buffer.from(value, 'base64'));
    await chmod(target, modes[name] === '100755' ? 0o755 : 0o644);
  }
}
