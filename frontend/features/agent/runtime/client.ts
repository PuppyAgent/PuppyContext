import { apiRequest, apiStreamRequest } from '@/lib/apiClient';
import { readAgentEvents } from './events';
import { AgentProtocolError } from './types';
import type { AgentRun, AgentSession, Submission } from './types';

/** Browser authentication and proxy only. Never creates or controls a sandbox. */
export class WebAgentClient {
  constructor(readonly projectId: string, readonly agentId: string) {}
  owned(run: AgentRun): AgentRun {
    if (!run || typeof run.id !== 'string' || typeof run.session_id !== 'string' ||
      typeof run.request_id !== 'string' || !Number.isSafeInteger(run.sequence) || run.sequence < 0 ||
      !run.snapshot || !['queued', 'running', 'waiting_approval', 'publishing', 'succeeded', 'stopped', 'failed', 'conflict', 'outcome_unknown'].includes(run.state)) {
      throw new AgentProtocolError('Cloud Agent returned an invalid run snapshot.');
    }
    if (run.project_id !== this.projectId || run.agent_id !== this.agentId) {
      throw new AgentProtocolError('Cloud Agent returned a different project or agent.');
    }
    return run;
  }
  async submit(input: Submission) {
    const run = this.owned(await apiRequest<AgentRun>('/api/v1/agents/runs', {
      method: 'POST', body: JSON.stringify({ ...input, project_id: this.projectId }),
    }));
    if (run.request_id !== input.request_id || (input.session_id && run.session_id !== input.session_id)) {
      throw new AgentProtocolError('Cloud Agent returned a different submission receipt.');
    }
    return run;
  }
  async receipt(request: string) {
    const run = this.owned(await apiRequest<AgentRun>(
      `/api/v1/agents/requests/${encodeURIComponent(this.projectId)}/${encodeURIComponent(request)}`));
    if (run.request_id !== request) throw new AgentProtocolError('Cloud Agent returned a different request.');
    return run;
  }
  async snapshot(id: string, signal?: AbortSignal) {
    const run = this.owned(await apiRequest<AgentRun>(`/api/v1/agents/runs/${encodeURIComponent(id)}`, { signal }));
    if (run.id !== id) throw new AgentProtocolError('Cloud Agent returned a different run.');
    return run;
  }
  async runs(session: string, signal?: AbortSignal, before?: string) {
    const query = new URLSearchParams({ limit: '100', ...(before ? { before } : {}) });
    const runs = await apiRequest<AgentRun[]>(`/api/v1/agents/sessions/${encodeURIComponent(session)}/runs?${query}`, { signal });
    for (const run of runs) {
      this.owned(run);
      if (run.session_id !== session) throw new AgentProtocolError('Cloud Agent returned a different session.');
    }
    return runs;
  }
  async sessions(signal?: AbortSignal) {
    const query = new URLSearchParams({ project_id: this.projectId, agent_id: this.agentId, limit: '200' });
    const rows = await apiRequest<AgentSession[]>(`/api/v1/agents/sessions?${query}`, { signal });
    return rows.filter(row => row.mode === 'cloud_pi' && row.agent_id === this.agentId);
  }
  async *events(run: string, after: number, signal: AbortSignal) {
    const response = await apiStreamRequest(`/api/v1/agents/runs/${encodeURIComponent(run)}/events?after=${after}`,
      { signal, headers: { Accept: 'text/event-stream' }, cache: 'no-store' });
    yield* readAgentEvents(response);
  }
  async stop(id: string) {
    const run = this.owned(await apiRequest<AgentRun>(`/api/v1/agents/runs/${encodeURIComponent(id)}/stop`, { method: 'POST' }));
    if (run.id !== id) throw new AgentProtocolError('Cloud Agent returned a different run.');
    return run;
  }
  async approve(id: string, call: string, decision: string, allow: boolean) {
    const run = this.owned(await apiRequest<AgentRun>(`/api/v1/agents/runs/${encodeURIComponent(id)}/approvals/${encodeURIComponent(call)}`,
      { method: 'POST', body: JSON.stringify({ decision_id: decision, allow }) }));
    if (run.id !== id) throw new AgentProtocolError('Cloud Agent returned a different run.');
    return run;
  }
}
