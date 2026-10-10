import type { MessagePart } from '@/components/chat/types';
import { runActive, type AgentRun } from './types';

export function projectRunMessages(runs: AgentRun[]) {
  return runs.flatMap(run => {
    const parts: MessagePart[] = (run.tools ?? []).map(tool => ({ type: 'tool', toolId: tool.call_id,
      toolName: tool.name, toolInput: JSON.stringify(tool.input),
      toolStatus: toolStatus(tool.state, runActive(run)),
      toolOutput: (tool.result?.content ?? []).filter(part => part.type === 'text').map(part => part.text ?? '').join('\n'),
    }));
    if (run.snapshot.text) parts.push({ type: 'text', content: run.snapshot.text });
    return [
      { id: `${run.id}:user`, role: 'user' as const, content: run.prompt, timestamp: new Date(run.created_at), parts: undefined, isStreaming: false },
      { id: `${run.id}:assistant`, role: 'assistant' as const, content: run.snapshot.text ?? '',
        timestamp: new Date(run.updated_at), parts, isStreaming: runActive(run) },
    ];
  });
}

export function runStatus(run?: AgentRun): string | null {
  if (!run) return null;
  switch (run.state) {
    case 'queued': return 'Waiting to start';
    case 'waiting_approval': return 'Waiting for your approval';
    case 'publishing': return 'Saving changes to Cloud';
    case 'stopped': return 'Stopped';
    case 'conflict': return 'Changes conflict with a newer Cloud version. Your work has been retained.';
    case 'outcome_unknown': return 'Save confirmation is unavailable. Check the project history before retrying.';
    case 'failed': return `Agent could not finish${run.snapshot.code ? ` (${run.snapshot.code})` : ''}.`;
    default: return null;
  }
}

function toolStatus(state: string, active: boolean): MessagePart['toolStatus'] {
  if (state === 'completed') return 'completed';
  if (state === 'rejected' || !active) return 'error';
  return 'running';
}
