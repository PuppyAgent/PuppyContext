/** HTTP byte relay over the provider channel; Git protocol belongs to stock Git/server. */
import { createServer } from 'node:http';
import { MAX_GIT_BYTES } from './git.mjs';
export async function gitRelay(request, project) {
  const prefix = `/git/${encodeURIComponent(project)}.git`;
  let enabled = false;
  const server = createServer(async (req,res) => {
    try {
      if (!enabled || !req.url.startsWith(prefix + '/')) throw new Error('Git operation is not active');
      const buffers = []; let bytes = 0;
      for await (const chunk of req) {
        bytes += chunk.length;
        if (bytes > MAX_GIT_BYTES) throw new Error('Git input limit exceeded');
        buffers.push(chunk);
      }
      const reply = await request('git_http', { method:req.method, path:req.url,
        protocol:req.headers['git-protocol'] || '', body:Buffer.concat(buffers).toString('base64') });
      res.writeHead(reply.status, {'content-type':reply.content_type,'cache-control':'no-store'});
      res.end(Buffer.from(reply.body,'base64'));
    } catch { res.writeHead(503).end('Git transport unavailable'); }
  });
  await new Promise(resolve => server.listen(0,'127.0.0.1',resolve));
  return { url:`http://127.0.0.1:${server.address().port}${prefix}`,
    async run(fn) { if (enabled) throw new Error('Concurrent Git commands denied'); enabled=true;
      try { return await fn(); } finally { enabled=false; } },
    close() { server.closeAllConnections(); server.close(); } };
}
