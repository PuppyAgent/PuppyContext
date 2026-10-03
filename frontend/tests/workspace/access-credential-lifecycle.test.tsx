import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import { CliCredentialIssuePanel } from '@/features/files/components/access-points/connect-methods/CliCredentialIssuePanel';
import { regenerateAccessSurfaceKey } from '@/lib/repoApi';
import type { AccessCredentialIssued } from '@puppyone/cloud-core';

const auth = vi.hoisted(() => ({ userId: 'user-1', session: { access_token: 'session-1' }, isAuthReady: true }));
vi.mock('@/contexts/SupabaseAuthProvider', () => ({ useAuth: () => auth }));
vi.mock('@/lib/repoApi', () => ({ regenerateAccessSurfaceKey: vi.fn() }));
const target = { kind: 'project_root' as const, project_id: 'p1' };
const credential = `cli_${'A'.repeat(40)}`;
const issued: AccessCredentialIssued = { access_surface_id: 'surface-1', target, credential };
function deferred() {
  let resolve!: (value: AccessCredentialIssued) => void;
  const promise = new Promise<AccessCredentialIssued>(done => { resolve = done; });
  return { promise, resolve };
}
beforeEach(() => {
  auth.userId = 'user-1'; auth.session = { access_token: 'session-1' }; auth.isAuthReady = true;
  vi.mocked(regenerateAccessSurfaceKey).mockReset();
});

it('reveals only an explicit issuance and clears it on identity change', async () => {
  vi.mocked(regenerateAccessSurfaceKey).mockResolvedValue(issued);
  const view = render(<CliCredentialIssuePanel connectorId="surface-1" target={target} />);
  expect(regenerateAccessSurfaceKey).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Generate new CLI key' }));
  await waitFor(() => expect(document.body.textContent).toContain(credential));
  expect(regenerateAccessSurfaceKey).toHaveBeenCalledExactlyOnceWith('surface-1');
  view.rerender(<CliCredentialIssuePanel connectorId="surface-2" target={target} />);
  expect(document.body.textContent).not.toContain(credential);
});

it.each(['target', 'user', 'session'] as const)('discards late issuance after %s context changes', async change => {
  const pending = deferred(); vi.mocked(regenerateAccessSurfaceKey).mockReturnValue(pending.promise);
  const view = render(<CliCredentialIssuePanel connectorId="surface-1" target={target} />);
  fireEvent.click(screen.getByRole('button', { name: 'Generate new CLI key' }));
  if (change === 'user') auth.userId = 'user-2';
  if (change === 'session') auth.session = { access_token: 'session-2' };
  view.rerender(<CliCredentialIssuePanel connectorId="surface-1" target={change === 'target' ? { ...target, project_id: 'p2' } : target} />);
  await act(async () => pending.resolve(issued));
  expect(document.body.textContent).not.toContain(credential);
  expect((screen.getByRole('button', { name: 'Generate new CLI key' }) as HTMLButtonElement).disabled).toBe(false);
});

it.each([
  { ...issued, access_surface_id: 'foreign-surface' },
  { ...issued, target: { ...target, project_id: 'foreign-project' } },
  { ...issued, credential: 'masked-legacy-key' },
])('never promotes foreign or masked metadata into a credential', async result => {
  vi.mocked(regenerateAccessSurfaceKey).mockResolvedValue(result);
  render(<CliCredentialIssuePanel connectorId="surface-1" target={target} />);
  fireEvent.click(screen.getByRole('button', { name: 'Generate new CLI key' }));
  await screen.findByText('Cloud returned an invalid one-time CLI credential');
  expect(document.body.textContent).not.toContain(credential);
  expect(document.body.textContent).not.toContain('masked-legacy-key');
});
