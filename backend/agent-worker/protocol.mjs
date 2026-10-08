/** Bounded JSON control frames. Model output never grants workspace capabilities. */
import { randomUUID } from 'node:crypto';
import { createInterface } from 'node:readline';
export const MAX_FRAME = 192 * 1024 * 1024;
export function channel(input = process.stdin, output = process.stdout) {
  const pending = new Map();
  let sequence = 0;
  const send = value => {
    const data = JSON.stringify({ ...value, sequence: ++sequence }) + '\n';
    if (Buffer.byteLength(data) > MAX_FRAME) throw new Error('Control frame exceeds limit');
    output.write(data);
  };
  const request = (type, payload = {}) => new Promise((resolve, reject) => {
    const id = randomUUID(); pending.set(id, { resolve, reject }); send({ type, id, ...payload });
  });
  const lines = createInterface({ input });
  const dispatch = callback => lines.on('line', line => {
    try {
      if (Buffer.byteLength(line) > MAX_FRAME) throw new Error('Control frame exceeds limit');
      const frame = JSON.parse(line);
      if (frame.type === 'reply' && pending.has(frame.id)) {
        const waiter = pending.get(frame.id); pending.delete(frame.id);
        frame.error ? waiter.reject(new Error(frame.error)) : waiter.resolve(frame);
      } else Promise.resolve(callback(frame)).catch(error => send({type:'failed', error:error.message}));
    } catch (error) { send({type:'failed', error:error.message}); }
  });
  const close = () => { for (const p of pending.values()) p.reject(new Error('Control disconnected')); pending.clear(); lines.close(); };
  return { send, request, dispatch, close };
}
