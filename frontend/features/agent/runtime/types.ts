/** Public durable-run protocol; credentials and sandbox ownership stay server-side. */
export type RunState = 'queued' | 'running' | 'waiting_approval' | 'publishing'
  | 'succeeded' | 'stopped' | 'failed' | 'conflict' | 'outcome_unknown';
export type AgentTool = {
  call_id: string; name: string; input: Record<string, unknown>;
  state: 'waiting' | 'approved' | 'rejected' | 'executing' | 'completed';
  result?: { content?: Array<{ type: string; text?: string }> } | null;
};
export type AgentRun = {
  id: string; project_id: string; agent_id: string; session_id: string; request_id: string;
  prompt: string; state: RunState; sequence: number; stop_requested: boolean;
  snapshot: { text?: string; code?: string; resource_retained?: boolean };
  publication: { status?: string; [key: string]: unknown } | null;
  tools?: AgentTool[]; created_at: string; updated_at: string;
};
export type AgentSession = {
  id: string; agent_id: string; title: string | null; mode: string;
  created_at: string; updated_at: string;
};
export type Submission = { request_id: string; prompt: string; session_id?: string; agent_id: string };
export type AgentEvent = { id: number; event: string; data: unknown };
export const runActive = (run?: AgentRun) => Boolean(run &&
  ['queued', 'running', 'waiting_approval', 'publishing'].includes(run.state));

export const runStopping = (run: AgentRun | undefined, resolving: string | null) =>
  Boolean(run?.stop_requested || resolving === 'stop');

export class AgentProtocolError extends Error {
  constructor(message: string) { super(message); this.name = 'AgentProtocolError'; }
}
