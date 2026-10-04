import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { SWRConfig } from 'swr';
import { BackgroundTaskNotifier, addPendingTasks, updateTaskProgress, updateTaskStatusById, replaceTaskId } from '@/components/BackgroundTaskNotifier';
import { TaskStatusWidget } from '@/components/TaskStatusWidget';
import { ValueRenderer } from '@/components/editors/table/components/ValueRenderer';
import { batchGetETLTaskStatus } from '@/lib/etlApi';

vi.mock('@/contexts/SupabaseAuthProvider', () => ({ useAuth: () => ({ userId: 'account-a', session: { access_token: 'token' } }) }));
vi.mock('@/lib/etlApi', async original => ({ ...await original<object>(), batchGetETLTaskStatus: vi.fn(), cancelETLTask: vi.fn() }));

beforeEach(() => { sessionStorage.clear(); vi.mocked(batchGetETLTaskStatus).mockReset(); });
const task = { taskId: 'tmp-1', projectId: 'p', tableId: 'table.json', filename: 'report.pdf', status: 'uploading' as const };
function mount() {
  return render(<SWRConfig value={{ provider: () => new Map() }}>
    <BackgroundTaskNotifier /><TaskStatusWidget />
    <ValueRenderer value={null} nodeKey="report.pdf" tableId="table.json" isExpanded={false} isExpandable={false} onChange={() => {}} onToggle={() => {}} onSelect={() => {}} />
  </SWRConfig>);
}

describe('task refresh behavior', () => {
  it('shows newly added uploads, live progress and terminal table placeholders', async () => {
    mount();
    act(() => addPendingTasks([task]));
    expect(await screen.findByText('report.pdf')).toBeTruthy();
    expect(screen.getByText('Processing…')).toBeTruthy();
    act(() => updateTaskProgress(task.taskId, 42));
    expect(screen.getByText('Uploading 42%')).toBeTruthy();
    act(() => replaceTaskId(task.taskId, 'real-1'));
    act(() => updateTaskStatusById('real-1', 'finalizing'));
    expect(batchGetETLTaskStatus).not.toHaveBeenCalled();
    act(() => updateTaskStatusById('real-1', 'completed'));
    expect(screen.queryByText('Processing…')).toBeNull();
    expect(screen.getByText('Completed')).toBeTruthy();
    fireEvent.click(screen.getByTitle('Clear all'));
    expect(screen.queryByText('report.pdf')).toBeNull();
  });

  it('polls real file tasks and publishes completion for file refresh', async () => {
    vi.mocked(batchGetETLTaskStatus).mockResolvedValue({ tasks: [{ task_id: 'real-1', status: 'completed' }], total: 1 } as never);
    const completed = vi.fn();
    window.addEventListener('etl-task-completed', completed);
    mount();
    act(() => addPendingTasks([{ ...task, taskId: 'real-1', status: 'pending' }]));
    await waitFor(() => expect(screen.getByText('Completed')).toBeTruthy());
    expect(batchGetETLTaskStatus).toHaveBeenCalledWith(['real-1'], 'token');
    expect(completed).toHaveBeenCalledTimes(1);
    window.removeEventListener('etl-task-completed', completed);
  });
});
