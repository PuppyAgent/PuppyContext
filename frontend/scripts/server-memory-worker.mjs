import http from 'node:http';
import { setImmediate as tick } from 'node:timers/promises';
import { writeHeapSnapshot } from 'node:v8';
import path from 'node:path';
import next from 'next';

// Isolated test worker, never a public diagnostics route. IPC is owned by the
// test parent; samples are taken after GC, not inferred from macOS footprint.
const dev = process.env.PUPPYONE_MEMORY_MODE === 'dev';
const app = next({ dev, dir: process.cwd(), hostname: '127.0.0.1', port: 0 });
let server;
let closing = false;
const measure = () => ({
  ...process.memoryUsage(),
  cpu: process.cpuUsage(),
  timestamp: Date.now(),
});
const watchdog = setInterval(() => process.send?.({ type: 'usage', ...measure() }), 1_000);
watchdog.unref();

async function shutdown() {
  if (closing) return;
  closing = true;
  clearInterval(watchdog);
  server?.closeAllConnections();
  if (server) await new Promise(resolve => server.close(resolve));
  await app.close();
  process.exit(0);
}
let queue = Promise.resolve();
process.on('message', message => {
  queue = queue.then(async () => {
    if (message.type === 'sample') {
      global.gc();
      await tick();
      global.gc();
      await tick();
      process.send?.({ type: 'sample', id: message.id, ...measure() });
    } else if (message.type === 'snapshot') {
      const file = writeHeapSnapshot(path.join(process.env.PUPPYONE_MEMORY_ARTIFACTS, 'retained.heapsnapshot'));
      process.send?.({ type: 'snapshot', id: message.id, file });
    } else if (message.type === 'stop') await shutdown();
  }).catch(error => {
    process.send?.({ type: 'fatal', message: String(error) });
    process.exit(1);
  });
});
process.once('SIGTERM', () => void shutdown());
process.once('SIGINT', () => void shutdown());
process.once('disconnect', () => void shutdown());
try {
  await app.prepare();
  server = http.createServer(app.getRequestHandler());
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  process.send?.({ type: 'ready', port: server.address().port });
} catch (error) {
  process.send?.({ type: 'fatal', message: String(error) });
  process.exit(1);
}
