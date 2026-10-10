import { expect, it } from 'vitest';
import { projectRunMessages, runStatus } from '@/features/agent/runtime/projection';
import { makeRun } from './fixtures';
it('uses stable run/request identities and retains published replies without a second message store', () => {
  const messages = projectRunMessages([makeRun({ state: 'succeeded', snapshot: { text: 'Saved' } })]);
  expect(messages.map(message => message.id)).toEqual(['run-1:user', 'run-1:assistant']);
  expect(messages[1].content).toBe('Saved'); expect(messages[1].isStreaming).toBe(false);
});
it('does not fabricate completed tools after stopping or failure', () => {
  const messages = projectRunMessages([makeRun({ state: 'stopped', tools: [
    { call_id: 'a', name: 'read', input: {}, state: 'completed' },
    { call_id: 'b', name: 'write', input: {}, state: 'executing' },
  ] })]);
  expect(messages[1].parts?.map(part => part.toolStatus)).toEqual(['completed', 'error']);
});
it('distinguishes publishing, conflict and unknown save outcomes from successful completion', () => {
  expect(runStatus(makeRun({ state: 'publishing' }))).toContain('Saving');
  expect(runStatus(makeRun({ state: 'conflict' }))).toContain('conflict');
  expect(runStatus(makeRun({ state: 'outcome_unknown' }))).toContain('confirmation');
  expect(runStatus(makeRun({ state: 'succeeded' }))).toBeNull();
});
