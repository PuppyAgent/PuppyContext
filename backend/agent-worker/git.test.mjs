import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, writeFile, readFile, rm, chmod, symlink, link } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { git, finalize, inspect } from './git.mjs';

async function fixture(format, fn) {
  const root=await mkdtemp(path.join(tmpdir(),'agent-git-v2-'));
  const config={object_format:format,target_ref:'refs/heads/knowledge',base_oid:null,readonly:false};
  try {
    await git(root,['init',`--object-format=${format}`,'--initial-branch=knowledge','.']);
    await inspect(root,config);
    await fn(root,config);
  } finally { await rm(root,{recursive:true,force:true}); }
}
for (const format of ['sha1','sha256']) {
  test(`${format}: reads and unchanged finalization create no commit`,()=>fixture(format,async(root,config)=>{
    const result=await finalize(root,config,'empty');
    assert.equal(result.tip,null); assert.equal(result.changed,false);
    assert.equal(result.automatic_commit,false);
  }));
  test(`${format}: files, executable mode, deletion and retries preserve exact history`,()=>fixture(format,async(root,config)=>{
    await writeFile(path.join(root,'中文.md'),'first');
    await writeFile(path.join(root,'run.sh'),'echo hello\n'); await chmod(path.join(root,'run.sh'),0o755);
    const first=await finalize(root,config,'first');
    assert.equal(first.automatic_commit,true);
    const retry=await finalize(root,config,'different retry message');
    assert.equal(retry.tip,first.tip); assert.equal(retry.automatic_commit,false);
    config.base_oid=first.tip;
    await rm(path.join(root,'中文.md')); await writeFile(path.join(root,'next.md'),'second');
    const second=await finalize(root,config,'second');
    assert.equal(await git(root,['rev-list','--count','HEAD']),'2');
    assert.equal(await git(root,['rev-parse','HEAD^']),first.tip);
    assert.match(await git(root,['ls-tree','HEAD','run.sh']),/^100755 /);
    assert.notEqual(second.tip,first.tip);
  }));
}
test('readonly workspace rejects modifications without committing',()=>fixture('sha1',async(root,config)=>{
  await writeFile(path.join(root,'note.md'),'dirty'); config.readonly=true;
  await assert.rejects(finalize(root,config,'denied'),/Read-only/);
  assert.equal((await inspect(root,config)).tip,null);
}));
test('a changed branch cannot redirect publication',()=>fixture('sha1',async(root,config)=>{
  await git(root,['symbolic-ref','HEAD','refs/heads/other']);
  await assert.rejects(finalize(root,config,'denied'),/selected branch/);
}));
test('untrusted Git hooks and config includes never run',()=>fixture('sha1',async(root,config)=>{
  const marker=path.join(root,'PWNED');
  await writeFile(path.join(root,'.git/hooks/pre-commit'),`#!/bin/sh\ntouch '${marker}'\n`);
  await chmod(path.join(root,'.git/hooks/pre-commit'),0o755);
  await writeFile(path.join(root,'.git/config'),'[include]\npath = /not-a-readable-config\n[core]\nhooksPath = .git/hooks\n');
  await writeFile(path.join(root,'note.md'),'safe');
  await finalize(root,config,'safe');
  await assert.rejects(readFile(marker),{code:'ENOENT'});
}));
test('metadata symlinks cannot be followed by trusted finalization',()=>fixture('sha1',async(root,config)=>{
  const outside=path.join(root,'outside'); await writeFile(outside,'do not overwrite');
  await rm(path.join(root,'.git/config')); await symlink(outside,path.join(root,'.git/config'));
  await assert.rejects(finalize(root,config,'denied'),/symlink/);
  assert.equal(await readFile(outside,'utf8'),'do not overwrite');
}));
test('sanitizing a hardlinked config never overwrites another file',()=>fixture('sha1',async(root,config)=>{
  const outside=path.join(root,'outside'); await writeFile(outside,'do not overwrite');
  await rm(path.join(root,'.git/config')); await link(outside,path.join(root,'.git/config'));
  await inspect(root,config);
  assert.equal(await readFile(outside,'utf8'),'do not overwrite');
}));
