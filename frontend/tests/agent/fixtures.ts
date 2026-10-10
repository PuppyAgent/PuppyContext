import type { AgentRun, AgentSession } from '@/features/agent/runtime/types';
export const makeRun = (patch: Partial<AgentRun> = {}): AgentRun => ({
  id: 'run-1', project_id: 'project-1', agent_id: 'agent-1', session_id: 'session-1', request_id: 'request-1',
  prompt: 'Read the project', state: 'queued', sequence: 0, stop_requested: false,
  snapshot: {}, publication: null, tools: [], created_at: '2026-10-10T12:00:00Z', updated_at: '2026-10-10T12:00:00Z', ...patch,
});
export const session: AgentSession = { id: 'session-1', agent_id: 'agent-1', title: 'Read the project',
  mode: 'cloud_pi', created_at: '2026-10-10T12:00:00Z', updated_at: '2026-10-10T12:00:00Z' };
export function eventResponse(frames: string[], split = false) {
  return new Response(new ReadableStream<Uint8Array>({ start(controller) {
    const bytes = new TextEncoder().encode(frames.join(''));
    if (split) for (const byte of bytes) controller.enqueue(Uint8Array.of(byte));
    else controller.enqueue(bytes);
    controller.close();
  } }), { headers: { 'Content-Type': 'text/event-stream' } });
}
