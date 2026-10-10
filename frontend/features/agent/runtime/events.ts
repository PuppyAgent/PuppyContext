import { AgentProtocolError, type AgentEvent } from './types';

const MAX_FRAME_BYTES = 1_000_000;

/** Parse SSE frames, not network chunks. UTF-8, CRLF and multiline data can split anywhere. */
export async function* readAgentEvents(response: Response): AsyncGenerator<AgentEvent[]> {
  if (!response.headers.get('content-type')?.includes('text/event-stream')) {
    void response.body?.cancel();
    throw new AgentProtocolError('Cloud Agent returned an invalid event stream.');
  }
  const reader = response.body?.getReader();
  if (!reader) throw new AgentProtocolError('Cloud Agent event stream has no body.');
  const decoder = new TextDecoder();
  let buffer = '';
  let lines: string[] = [];
  let frameBytes = 0;
  let finished = false;
  const frame = (): AgentEvent | null => {
    const data = lines.filter(line => line.startsWith('data:')).map(line => line.slice(5).replace(/^ /, '')).join('\n');
    const id = lines.find(line => line.startsWith('id:'))?.slice(3).trim();
    const event = lines.find(line => line.startsWith('event:'))?.slice(6).trim();
    lines = []; frameBytes = 0;
    if (!data) return null; // comments/keepalives never imply completion
    if (!id || !/^\d+$/.test(id) || !Number.isSafeInteger(Number(id)) || !event) {
      throw new AgentProtocolError('Cloud Agent returned an invalid event cursor.');
    }
    try { return { id: Number(id), event, data: JSON.parse(data) }; }
    catch { throw new AgentProtocolError('Cloud Agent returned malformed event data.'); }
  };
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) { finished = true; break; } // discard incomplete frame; replay from confirmed cursor
      buffer += decoder.decode(value, { stream: true });
      const batch: AgentEvent[] = [];
      let end: number;
      while ((end = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, end).replace(/\r$/, '');
        buffer = buffer.slice(end + 1);
        frameBytes += line.length;
        if (frameBytes > MAX_FRAME_BYTES) throw new AgentProtocolError('Cloud Agent event exceeds the display limit.');
        const event = line === '' ? frame() : null;
        if (line !== '') lines.push(line);
        if (event) batch.push(event);
      }
      if (buffer.length + frameBytes > MAX_FRAME_BYTES) throw new AgentProtocolError('Cloud Agent event exceeds the display limit.');
      if (batch.length) yield batch;
    }
  } finally {
    if (!finished) await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
