import { describe, expect, it } from 'vitest';
import { readAgentEvents } from '@/features/agent/runtime/events';
import { eventResponse } from './fixtures';

async function collect(response: Response) {
  const events = [];
  for await (const batch of readAgentEvents(response)) events.push(...batch);
  return events;
}
describe('durable Agent SSE transport', () => {
  it('handles byte-split UTF-8, CRLF, multiline JSON, and keepalives', async () => {
    const events = await collect(eventResponse([': keepalive\r\n\r\nid: 4\r\nevent: text\r\ndata: {\r\ndata: "delta":"你好"}\r\n\r\n'], true));
    expect(events).toEqual([{ id: 4, event: 'text', data: { delta: '你好' } }]);
  });
  it('batches frames and discards a truncated final frame for replay', async () => {
    expect(await collect(eventResponse(['id: 1\nevent: text\ndata: {"delta":"A"}\n\n',
      'id: 2\nevent: text\ndata: {"delta":"B"}']))).toHaveLength(1);
  });
  it('does not interpret empty EOF as a completed task', async () => {
    expect(await collect(eventResponse([': keepalive\n\n']))).toEqual([]);
  });
  it.each(['bad', '9007199254740992', '-1'])('rejects invalid cursor %s', async id => {
    await expect(collect(eventResponse([`id: ${id}\nevent: text\ndata: {}\n\n`]))).rejects.toThrow('cursor');
  });
  it('rejects oversized frames and non-SSE bodies', async () => {
    await expect(collect(eventResponse(['data: ' + 'x'.repeat(1_000_001)]))).rejects.toThrow('limit');
    await expect(collect(new Response('{}'))).rejects.toThrow('invalid event stream');
  });
});
