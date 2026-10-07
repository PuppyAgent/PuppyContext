import { expect, it } from 'vitest';
import type { EntrypointBadge } from '@/features/files/DataLayoutContext';
import { entrypointBadgeKey, selectSynchronizeBadges } from '@/features/files/entrypointBadges';
import { endpointToPanelState, getEndpointEntries } from '@/features/files/components/access-points/utils';
import { createProjectSession } from '@/features/workspace/session';

const metadata = { id: 'same-id', provider: 'mcp', direction: 'inbound', status: 'active' };
const access: EntrypointBadge = { ...metadata, resourceKind: 'access' };
const binding: EntrypointBadge = { ...metadata, resourceKind: 'synchronize' };

it.each([[access, binding], [binding, access]])('keeps distinct domains at the same path and opaque ID', (...badges) => {
  const entries = new Map([['', badges]]);
  expect(entrypointBadgeKey(access)).not.toBe(entrypointBadgeKey(binding));
  expect(selectSynchronizeBadges(entries).get('')).toEqual(binding);
  const rows = getEndpointEntries(entries, { agents: {}, nodes: {}, syncs: { 'same-id': 'Source name' } });
  expect(rows).toHaveLength(2);
  expect(rows.find(row => row.ep.resourceKind === 'access')?.name).toBe('mcp');
  expect(rows.find(row => row.ep.resourceKind === 'synchronize')?.name).toBe('Source name');
});

it('never returns an Access identity from a binding selector', () => {
  expect(selectSynchronizeBadges(new Map([['folder', [access]]])).size).toBe(0);
  expect(endpointToPanelState(access, 'folder')).toEqual({ type: 'mcp_config', nodeId: 'folder', mcpEndpointId: 'same-id' });
  // Even a Provider label shared with Access cannot change the resource owner.
  expect(endpointToPanelState(binding, 'folder')).toEqual({ type: 'sync_config', nodeId: 'folder', synchronizeBindingId: 'same-id' });
});

it.each(['cli', 'git_remote', 'unrecognized'])('routes %s Access to management, never Synchronize', provider => {
  expect(endpointToPanelState({ ...access, provider }, '').type).toBe('access_list');
});

it('preserves the selected binding when multiple bindings share a path', () => {
  const second: EntrypointBadge = { ...binding, id: 'second-binding' };
  const session = createProjectSession();
  session.getState().openPanel(endpointToPanelState(second, 'same/path'));
  expect(session.getState().panel.synchronizeBindingId).toBe('second-binding');
  expect(selectSynchronizeBadges(new Map([['same/path', [access, binding, second]]])).get('same/path')?.id).toBe(binding.id);
});

it('deduplicates repeated views only within their actual resource domain', () => {
  const rows = getEndpointEntries(new Map([['one', [access, binding]], ['two', [access, binding]]]), { agents: {}, nodes: {}, syncs: {} });
  expect(rows).toHaveLength(2);
});
