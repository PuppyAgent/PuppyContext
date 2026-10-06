import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { captureGit, commitGit, git, knowledgeFiles, restoreFiles, restoreGit } from './git-workspace.mjs';

for (const format of ['sha1', 'sha256']) {
  test(`clone and recover full ${format} history, staged bytes and dirty files`, async () => {
    const parent = await mkdtemp(path.join(tmpdir(), 'puppyone-agent-git-'));
    try {
      const source = path.join(parent, 'source'), restored = path.join(parent, 'restored');
      await mkdir(source);
      await git(source, ['init', `--object-format=${format}`, '-b', 'writing']);
      await git(source, ['config', 'user.name', 'Fixture']);
      await git(source, ['config', 'user.email', 'fixture@puppyone.invalid']);
      await writeFile(path.join(source, 'note.md'), 'original');
      await commitGit(source, 'first');
      const first = (await captureGit(source)).tip;
      await git(source, ['checkout', '--orphan', 'independent']);
      await writeFile(path.join(source, 'note.md'), 'other history');
      await commitGit(source, 'independent');
      const independent = (await captureGit(source)).tip;
      await git(source, ['checkout', 'writing']);
      await writeFile(path.join(source, 'note.md'), 'staged');
      await git(source, ['add', 'note.md']);
      await writeFile(path.join(source, 'note.md'), 'dirty');
      await writeFile(path.join(source, 'new.md'), 'untracked');
      await writeFile(path.join(source, '__proto__'), 'ordinary knowledge file');
      const state = await captureGit(source), workspace = await knowledgeFiles(source);
      assert.ok(!Object.keys(workspace.files).some(name => name.startsWith('.git/')));
      assert.equal(Buffer.from(workspace.files['__proto__'], 'base64').toString(), 'ordinary knowledge file');
      await restoreGit(restored, state);
      await restoreFiles(restored, workspace.files, workspace.modes);
      assert.equal((await git(restored, ['show', ':note.md'])).stdout.toString(), 'staged');
      assert.equal(await readFile(path.join(restored, 'note.md'), 'utf8'), 'dirty');
      assert.equal((await git(restored, ['rev-parse', 'independent'])).stdout.toString().trim(), independent);
      await commitGit(restored, 'automatic save');
      const final = await captureGit(restored);
      assert.notEqual(final.tip, first);
      assert.equal((await git(restored, ['rev-parse', 'HEAD^'])).stdout.toString().trim(), first);
      assert.equal((await git(restored, ['status', '--porcelain'])).stdout.length, 0);
      await commitGit(restored, 'no empty commit');
      assert.equal((await captureGit(restored)).tip, final.tip);
    } finally { await rm(parent, { recursive: true, force: true }); }
  });
}

test('unborn HEAD preserves a partially staged first file', async () => {
  const parent = await mkdtemp(path.join(tmpdir(), 'puppyone-agent-empty-'));
  try {
    const source = path.join(parent, 'source'), restored = path.join(parent, 'restored');
    await mkdir(source);
    await git(source, ['init', '-b', 'knowledge']);
    await writeFile(path.join(source, 'note.md'), 'staged first');
    await git(source, ['add', 'note.md']);
    await writeFile(path.join(source, 'note.md'), 'dirty first');
    const state = await captureGit(source), workspace = await knowledgeFiles(source);
    await restoreGit(restored, state);
    await restoreFiles(restored, workspace.files);
    assert.equal((await git(restored, ['show', ':note.md'])).stdout.toString(), 'staged first');
    await commitGit(restored, 'first automatic save');
    assert.equal((await git(restored, ['branch', '--show-current'])).stdout.toString().trim(), 'knowledge');
    assert.equal((await git(restored, ['show', 'HEAD:note.md'])).stdout.toString(), 'dirty first');
  } finally { await rm(parent, { recursive: true, force: true }); }
});
