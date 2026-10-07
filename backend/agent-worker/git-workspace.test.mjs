import test from 'node:test';
import assert from 'node:assert/strict';
import { chmod, mkdtemp, mkdir, writeFile, readFile, rename, rm, symlink } from 'node:fs/promises';
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

for (const format of ['sha1', 'sha256']) {
  test(`repeated ${format} recovery retains merge parents, annotated tags and file changes`, async () => {
    const parent = await mkdtemp(path.join(tmpdir(), 'puppyone-agent-history-'));
    try {
      let root = path.join(parent, 'source');
      await restoreGit(root, { object_format: format, head: Buffer.from('refs/heads/写作').toString('base64'), tip: null });
      await writeFile(path.join(root, 'draft.md'), 'original');
      await writeFile(path.join(root, 'remove.md'), 'obsolete');
      await commitGit(root, 'initial');
      const initial = (await captureGit(root)).tip;
      await git(root, ['checkout', '-b', 'research']);
      await writeFile(path.join(root, 'research.md'), 'evidence');
      await commitGit(root, 'research');
      const research = (await captureGit(root)).tip;
      await git(root, ['checkout', '写作']);
      await writeFile(path.join(root, 'draft.md'), 'revised');
      await commitGit(root, 'writing');
      const writing = (await captureGit(root)).tip;
      await git(root, ['merge', '--no-ff', 'research', '-m', 'merge evidence']);
      const merge = (await captureGit(root)).tip;
      await git(root, ['tag', '-a', 'reviewed', '-m', 'reviewed knowledge']);
      const tag = (await git(root, ['rev-parse', 'refs/tags/reviewed'])).stdout.toString().trim();
      await rename(path.join(root, 'draft.md'), path.join(root, '章节 one\n二.md'));
      await rm(path.join(root, 'remove.md'));
      const binary = Buffer.from([0, 255, 128, 13, 10, 0, 42]);
      await writeFile(path.join(root, 'attachment.bin'), binary);
      await writeFile(path.join(root, 'script.sh'), '#!/bin/sh\nexit 0\n');
      await chmod(path.join(root, 'script.sh'), 0o755);
      await writeFile(path.join(root, '.gitignore'), '*.scratch\n');
      await writeFile(path.join(root, 'private.scratch'), 'retained only in recovery');
      // Two destroyed-sandbox recoveries must preserve dirty work as well as
      // the complete graph; finally committing must not rewrite prior objects.
      for (let cycle = 0; cycle < 2; cycle++) {
        const state = await captureGit(root), workspace = await knowledgeFiles(root);
        const next = path.join(parent, `restored-${cycle}`);
        await restoreGit(next, state);
        await restoreFiles(next, workspace.files, workspace.modes);
        await rm(root, { recursive: true, force: true });
        root = next;
        assert.equal((await captureGit(root)).tip, merge);
        assert.equal((await git(root, ['rev-list', '--parents', '-n', '1', merge])).stdout.toString().trim(), `${merge} ${writing} ${research}`);
        assert.equal((await git(root, ['rev-parse', 'reviewed'])).stdout.toString().trim(), tag);
        assert.equal((await git(root, ['show', `${initial}:draft.md`])).stdout.toString(), 'original');
        assert.deepEqual(await readFile(path.join(root, 'attachment.bin')), binary);
        assert.equal((await knowledgeFiles(root)).modes['script.sh'], '100755');
        assert.equal(await readFile(path.join(root, 'private.scratch'), 'utf8'), 'retained only in recovery');
        await assert.rejects(git(root, ['config', '--get', 'remote.origin.url']));
      }
      await commitGit(root, 'automatic save');
      const saved = (await captureGit(root)).tip;
      assert.equal((await git(root, ['rev-parse', 'HEAD^'])).stdout.toString().trim(), merge);
      assert.equal((await git(root, ['show', 'HEAD:章节 one\n二.md'])).stdout.toString(), 'revised');
      assert.deepEqual((await git(root, ['show', 'HEAD:attachment.bin'])).stdout, binary);
      assert.match((await git(root, ['ls-tree', 'HEAD', 'script.sh'])).stdout.toString(), /^100755 /);
      for (const name of ['draft.md', 'remove.md', 'private.scratch'])
        await assert.rejects(git(root, ['cat-file', '-e', `HEAD:${name}`]));
      await commitGit(root, 'unchanged turn');
      assert.equal((await captureGit(root)).tip, saved);
      await git(root, ['fsck', '--full']);
    } finally { await rm(parent, { recursive: true, force: true }); }
  });
}

test('automatic commit cannot invoke a repository hook', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'puppyone-agent-hooks-'));
  try {
    await restoreGit(root, { object_format: 'sha1', head: Buffer.from('refs/heads/main').toString('base64'), tip: null });
    await writeFile(path.join(root, '.git/hooks/pre-commit'), '#!/bin/sh\nprintf injected > hook-ran\nexit 1\n');
    await chmod(path.join(root, '.git/hooks/pre-commit'), 0o755);
    await writeFile(path.join(root, 'note.md'), 'safe');
    await commitGit(root, 'automatic save');
    await assert.rejects(readFile(path.join(root, 'hook-ran')), { code: 'ENOENT' });
    assert.equal((await git(root, ['show', 'HEAD:note.md'])).stdout.toString(), 'safe');
  } finally { await rm(root, { recursive: true, force: true }); }
});

test('corrupt pack cannot be restored as a healthy workspace', async () => {
  const parent = await mkdtemp(path.join(tmpdir(), 'puppyone-agent-corrupt-'));
  try {
    const root = path.join(parent, 'source');
    await restoreGit(root, { object_format: 'sha1', head: Buffer.from('refs/heads/main').toString('base64'), tip: null });
    await writeFile(path.join(root, 'note.md'), 'must survive');
    await commitGit(root, 'saved');
    const state = await captureGit(root);
    const data = Buffer.from(state.bundle, 'base64');
    data[data.length - 1] ^= 0xff;
    await assert.rejects(restoreGit(path.join(parent, 'restored'), { ...state, bundle: data.toString('base64') }));
    assert.equal((await git(root, ['show', 'HEAD:note.md'])).stdout.toString(), 'must survive');
  } finally { await rm(parent, { recursive: true, force: true }); }
});

test('checkpoint rejects symlinks and paths that could overwrite Git metadata', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'puppyone-agent-paths-'));
  try {
    await writeFile(path.join(root, 'original.md'), 'original');
    await symlink('original.md', path.join(root, 'linked.md'));
    await assert.rejects(knowledgeFiles(root), /Unsupported workspace file type/);
    await rm(path.join(root, 'linked.md'));
    for (const name of ['../escape', '/tmp/escape', '.git/config', 'folder/.GiT/config', 'a/../original.md'])
      await assert.rejects(restoreFiles(root, { [name]: Buffer.from('overwrite').toString('base64') }), /Invalid checkpoint path/);
    assert.equal(await readFile(path.join(root, 'original.md'), 'utf8'), 'original');
  } finally { await rm(root, { recursive: true, force: true }); }
});
