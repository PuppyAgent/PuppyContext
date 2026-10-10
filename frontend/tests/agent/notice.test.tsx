import { fireEvent, render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { AgentRunNotice } from '@/features/agent/components/AgentRunNotice';
import { EMPTY_STATE, type WebAgentController } from '@/features/agent/runtime/controller';
import { makeRun } from './fixtures';
it('shows explicit per-tool approval actions instead of granting automatically', () => {
  const approve = vi.fn();
  render(<AgentRunNotice controller={{ approve } as unknown as WebAgentController} state={{ ...EMPTY_STATE,
    runs: [makeRun({ state: 'waiting_approval', tools: [{ call_id: 'tool-1', name: 'bash', input: { command: 'mkdir notes' }, state: 'waiting' }] })] }} />);
  expect(screen.getByText('mkdir notes')).toBeTruthy(); expect(approve).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Allow' })); expect(approve).toHaveBeenCalledWith('tool-1', true);
  fireEvent.click(screen.getByRole('button', { name: 'Decline' })); expect(approve).toHaveBeenCalledWith('tool-1', false);
});
it('uses the frozen pending prompt to confirm an uncertain submission', () => {
  const submit = vi.fn();
  render(<AgentRunNotice controller={{ submit } as unknown as WebAgentController} state={{ ...EMPTY_STATE,
    draft: 'New draft', pending: { request_id: 'original', prompt: 'Original request', agent_id: 'agent-1' } }} />);
  fireEvent.click(screen.getByRole('button', { name: 'Confirm message' })); expect(submit).toHaveBeenCalledWith('Original request');
});
it('does not offer approval actions for a terminal run', () => {
  render(<AgentRunNotice controller={null} state={{ ...EMPTY_STATE,
    runs: [makeRun({ state: 'stopped', tools: [{ call_id: 'tool-1', name: 'write', input: {}, state: 'waiting' }] })] }} />);
  expect(screen.queryByRole('button', { name: 'Allow' })).toBeNull(); expect(screen.getByText('Stopped')).toBeTruthy();
});
