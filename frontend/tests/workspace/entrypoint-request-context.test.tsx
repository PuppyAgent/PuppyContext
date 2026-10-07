import { act, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useEntrypointRequestContext } from '@/lib/hooks/useEntrypointRequestContext';
import { SupabaseConnectDialog } from '@/components/SupabaseConnectDialog';
import { SupabaseSQLEditorDialog } from '@/components/SupabaseSQLEditorDialog';

const state = vi.hoisted(() => ({ auth: { userId: 'user-1', session: { access_token: 'session-1' }, isAuthReady: true }, create: vi.fn(), list: vi.fn(), preview: vi.fn(), save: vi.fn() }));
vi.mock('@/contexts/SupabaseAuthProvider', () => ({ useAuth: () => state.auth }));
vi.mock('@/lib/importDatabaseApi', () => ({ createImportDatabaseSource: state.create, listImportDatabaseTables: state.list, previewImportDatabaseTable: state.preview, saveImportDatabaseTable: state.save }));
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done; }); return { promise, resolve }; }

beforeEach(() => {
  state.auth = { userId: 'user-1', session: { access_token: 'session-1' }, isAuthReady: true };
  vi.clearAllMocks();
});

describe('entrypoint request UI context', () => {
  it('rejects late completions across Project/resource/user/session and unmount', () => {
    const { result, rerender, unmount } = renderHook(({ project, resource }) => useEntrypointRequestContext(project, resource), { initialProps: { project: 'p1', resource: 's1' } });
    let valid = result.current(); expect(valid()).toBe(true);
    rerender({ project: 'p2', resource: 's1' }); expect(valid()).toBe(false);
    valid = result.current(); rerender({ project: 'p2', resource: 's2' }); expect(valid()).toBe(false);
    valid = result.current(); state.auth = { ...state.auth, userId: 'user-2' }; rerender({ project: 'p2', resource: 's2' }); expect(valid()).toBe(false);
    valid = result.current(); state.auth = { ...state.auth, session: { access_token: 'session-2' } }; rerender({ project: 'p2', resource: 's2' }); expect(valid()).toBe(false);
    valid = result.current(); expect(valid()).toBe(true); unmount(); expect(valid()).toBe(false);
  });

  it('does not open another Project table picker from a late source creation', async () => {
    const request = deferred<{ source: { id: string } }>(); state.create.mockReturnValue(request.promise);
    const connected = vi.fn();
    const { rerender } = render(<SupabaseConnectDialog projectId="p1" onClose={vi.fn()} onConnected={connected} />);
    fireEvent.change(screen.getByPlaceholderText('https://your-project.supabase.co'), { target: { value: 'https://source.example.test' } });
    fireEvent.change(screen.getByPlaceholderText('eyJhbGciOiJIUzI1NiIs...'), { target: { value: 'test-secret' } });
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }));
    expect(state.create).toHaveBeenCalledWith('p1', expect.objectContaining({ api_key: 'test-secret' }));
    rerender(<SupabaseConnectDialog projectId="p2" onClose={vi.fn()} onConnected={connected} />);
    await act(async () => request.resolve({ source: { id: 'p1-source' } }));
    expect(connected).not.toHaveBeenCalled();
    expect((screen.getByPlaceholderText('eyJhbGciOiJIUzI1NiIs...') as HTMLInputElement).value).toBe('');
  });

  it('does not replace a selected table preview with a delayed prior response', async () => {
    state.list.mockResolvedValue([{ name: 'first', type: 'table', columns: [] }, { name: 'second', type: 'table', columns: [] }]);
    const old = deferred<unknown>();
    state.preview.mockImplementation((_id, name) => name === 'first' ? old.promise : Promise.resolve({ columns: ['value'], rows: [{ value: 'SECOND-DATA' }], row_count: 1, execution_time_ms: 1 }));
    render(<SupabaseSQLEditorDialog projectId="p1" importDatabaseSourceId="source-1" onClose={vi.fn()} />);
    fireEvent.click(await screen.findByText('first', { exact: true }));
    await waitFor(() => expect(state.preview).toHaveBeenCalledWith('source-1', 'first', 50));
    fireEvent.click(screen.getByText('second', { exact: true }));
    expect(await screen.findByText('SECOND-DATA')).toBeTruthy();
    await act(async () => old.resolve({ columns: ['value'], rows: [{ value: 'STALE-DATA' }], row_count: 1, execution_time_ms: 1 }));
    expect(screen.queryByText('STALE-DATA')).toBeNull();
    expect(screen.getByText('SECOND-DATA')).toBeTruthy();
  });
});
