/** Pi 0.85.1 ships npm-shrinkwrap.json. npm ci re-expands that subtree even
 * with root overrides. Apply the reviewed, integrity-pinned patch releases
 * explicitly, and verify the installed tree instead of trusting lock metadata.
 * No Pi source or public SDK version changes.
 */
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { mkdtemp, readFile, rm, mkdir, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
const patches = [
  ['undici', '8.10.2', '/y4/bH9YNU5hi9NIrpOuvGXFcxrj3CMrV+/AYpowAYTpHn8gX/XPFjNy766FPoYY0miQhdW977JFWKGNhBdwyQ=='],
  ['brace-expansion', '5.0.12', 'YovQ3rzhaLMIrDjNDMkNS01tea93qhEhG5xy8f6+R0l+dw3Ki+5sCoIoI942iuLZTHWogWktgwVDhU09iNEimQ=='],
];
const root = path.resolve('node_modules/@earendil-works/pi-coding-agent/node_modules');
for (const [name, version, checksum] of patches) {
  const target = path.join(root, name);
  const current = JSON.parse(await readFile(path.join(target, 'package.json'), 'utf8'));
  if (current.version === version) continue;
  const response = await fetch(`https://registry.npmjs.org/${name}/-/${name}-${version}.tgz`);
  if (!response.ok) throw new Error(`Patch download failed for ${name}`);
  const bytes = Buffer.from(await response.arrayBuffer());
  if (createHash('sha512').update(bytes).digest('base64') !== checksum) throw new Error('Patch integrity mismatch');
  const staging = await mkdtemp(path.join(tmpdir(), 'pi-dependency-'));
  try {
    const archive = path.join(staging, 'package.tgz');
    await writeFile(archive, bytes);
    await rm(target, {recursive: true});
    await mkdir(target);
    execFileSync('tar', ['-xzf', archive, '--strip-components=1', '-C', target]);
    const installed = JSON.parse(await readFile(path.join(target, 'package.json'), 'utf8'));
    if (installed.name !== name || installed.version !== version) throw new Error('Patch version mismatch');
  } finally { await rm(staging, {recursive: true}); }
}
console.log('Verified dependency patches: undici 8.10.2; brace-expansion 5.0.12');
