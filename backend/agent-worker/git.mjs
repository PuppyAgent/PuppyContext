/** Stock Git operations. No object codecs, file inventories or cloud credentials. */
import { execFile } from 'node:child_process';
import { lstat, writeFile, rename, chown } from 'node:fs/promises';
import { randomUUID } from 'node:crypto';
import path from 'node:path';
export const ROOT = '/workspace/repo';
export const MAX_GIT_BYTES = 128 * 1024 * 1024;
export function git(root, args) {
  return new Promise((resolve, reject) => {
    execFile('git', ['-c','core.hooksPath=/dev/null','-c','core.fsmonitor=false',
      '-c','commit.gpgSign=false','-c','maintenance.auto=false','-c','gc.auto=0',
      '-c','protocol.file.allow=never','-c','protocol.ext.allow=never',
      '-c','http.followRedirects=false','-c','credential.helper=', ...args], {
      cwd: root, uid: process.getuid() === 0 ? 1000 : undefined,
      gid: process.getuid() === 0 ? 1000 : undefined,
      env: { PATH: '/usr/local/bin:/usr/bin:/bin', HOME: '/home/node',
        GIT_CONFIG_NOSYSTEM:'1', GIT_CONFIG_GLOBAL:'/dev/null', GIT_TERMINAL_PROMPT:'0',
        GIT_NO_REPLACE_OBJECTS:'1', GIT_OPTIONAL_LOCKS:'0' },
      encoding:'utf8', maxBuffer:MAX_GIT_BYTES, timeout:120000,
    }, (error, stdout, stderr) => {
      if (error) { error.output = stdout; error.stderr = stderr; reject(error); }
      else resolve(stdout.trimEnd());
    });
  });
}
export async function exists(name) {
  try { return await lstat(name); } catch (error) { if (error.code === 'ENOENT') return null; throw error; }
}
function validate(config) {
  if (!['sha1','sha256'].includes(config.object_format) || !/^refs\/heads\//.test(config.target_ref))
    throw new Error('Invalid workspace target');
}
export async function protectConfig(root, config) {
  validate(config);
  const dot = await lstat(path.join(root,'.git'));
  if (!dot.isDirectory() || dot.isSymbolicLink()) throw new Error('Invalid Git directory');
  for (const name of ['config','HEAD','index','objects','refs']) {
    const stat = await exists(path.join(root,'.git',name));
    if (stat?.isSymbolicLink()) throw new Error('Invalid Git metadata symlink');
  }
  for (const name of ['commondir','shallow','objects/info/alternates','objects/info/http-alternates'])
    if (await exists(path.join(root,'.git',name))) throw new Error('External or shallow Git metadata denied');
  // Config is model-writable. Never evaluate includes, helpers, filters or URL rewrites from it.
  const temporary = path.join(root,'.git',`config-${randomUUID()}`);
  await writeFile(temporary,
    `[core]\nrepositoryformatversion = ${config.object_format === 'sha256' ? 1 : 0}\nfilemode = true\nbare = false\n` +
    (config.object_format === 'sha256' ? '[extensions]\nobjectformat = sha256\n' : '') +
    '[user]\nname = Puppyone Agent\nemail = agent@puppyone.invalid\n', { flag:'wx', mode:0o600 });
  if (process.getuid() === 0) await chown(temporary,1000,1000);
  await rename(temporary,path.join(root,'.git/config'));
}
export async function inspect(root, config) {
  await protectConfig(root, config);
  const head = await git(root,['symbolic-ref','-q','HEAD']);
  if (head !== config.target_ref) throw new Error('Agent changed its selected branch');
  let tip = null;
  try { tip = await git(root,['rev-parse','--verify','HEAD']); }
  catch (error) { if (error.code !== 128) throw error; }
  return { object_format: config.object_format, target_ref: head, tip };
}
export async function prepare(root, config, remote, { reuse = false } = {}) {
  validate(config);
  if (!(await exists(path.join(root,'.git')))) {
    await git(root,['init',`--object-format=${config.object_format}`,'--initial-branch',config.target_ref.slice(11),'.']);
  } else {
    await protectConfig(root, config);
    if (reuse && await git(root,['status','--porcelain','--untracked-files=all']))
      throw new Error('Cannot synchronize a dirty retained workspace');
  }
  if (config.base_oid) {
    await git(root,['fetch','--no-tags',remote,`+${config.target_ref}:refs/remotes/origin/selected`]);
    const fetched = await git(root,['rev-parse','refs/remotes/origin/selected']);
    if (fetched !== config.base_oid) throw new Error('Cloud branch changed while preparing workspace');
    const current = await inspect(root,config);
    if (!current.tip) await git(root,['checkout','-B',config.target_ref.slice(11),fetched]);
    else if (current.tip !== fetched) await git(root,['merge','--ff-only',fetched]);
  } else if ((await inspect(root,config)).tip) throw new Error('Retained workspace has unpublished history');
  return inspect(root,config);
}
export async function finalize(root, config, message) {
  const before = await inspect(root,config);
  if (!config.readonly) {
    await git(root,['add','-A','--','.']);
    try { await git(root,['diff','--cached','--quiet','--exit-code']); }
    catch (error) {
      if (error.code !== 1) throw error;
      await git(root,['commit','--no-gpg-sign','-m',message]);
    }
  } else if (await git(root,['status','--porcelain','--untracked-files=all'])) {
    throw new Error('Read-only workspace was modified');
  }
  const result = await inspect(root,config);
  if (result.tip !== config.base_oid && config.base_oid)
    await git(root,['merge-base','--is-ancestor',config.base_oid,result.tip]);
  return { ...result, changed: result.tip !== config.base_oid, automatic_commit: before.tip !== result.tip };
}
export async function push(root, config, remote, candidate) {
  const state = await inspect(root,config);
  if (state.tip !== candidate || !candidate || config.readonly) throw new Error('Publication candidate changed');
  try {
    return {output:await git(root,['push','--porcelain','--no-verify',remote,`${candidate}:${config.target_ref}`])};
  } catch (error) {
    // Porcelain explicitly proves a rejected ref. Network/ACK errors remain unknown.
    if (/^!\t[^\n]+\t\[rejected\] \((fetch first|non-fast-forward)\)$/m.test(error.output || ''))
      return {rejected:true};
    throw error;
  }
}
