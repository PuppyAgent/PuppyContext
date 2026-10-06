/** Cloud Pi harness. stdin/stdout is the only control/model channel. */
import { createServer } from 'node:http';
import { createInterface } from 'node:readline';
import { randomUUID } from 'node:crypto';
import { mkdir, writeFile, readdir, readFile, lstat, realpath } from 'node:fs/promises';
import path from 'node:path';
import {
  createAgentSession, createExtensionRuntime, ModelRuntime, SessionManager,
  SettingsManager, VERSION, createReadToolDefinition, createWriteToolDefinition,
  createEditToolDefinition, createBashToolDefinition, createLsToolDefinition,
  createFindToolDefinition, createGrepToolDefinition,
} from '@earendil-works/pi-coding-agent';

const root = process.cwd();
const maxBytes = 64 * 1024 * 1024;
const pending = new Map();
const models = new Map();
let session;
let manager;
let stopped = false;
let config;
const send = (value) => process.stdout.write(`${JSON.stringify(value)}\n`);
const request = (type, payload) => new Promise((resolve, reject) => {
  const id = randomUUID();
  pending.set(id, { resolve, reject });
  send({ type, id, ...payload });
});

async function safePath(value, allowMissing = false) {
  if (typeof value !== 'string' || value.includes('\0')) throw new Error('Invalid workspace path');
  const target = path.resolve(root, value);
  if (target !== root && !target.startsWith(`${root}/`)) throw new Error('Path outside assigned workspace');
  let check = target;
  for (;;) {
    try {
      const resolved = await realpath(check);
      if (resolved !== root && !resolved.startsWith(`${root}/`)) throw new Error('Workspace symlink escape');
      break;
    } catch (error) {
      if (!allowMissing || error.code !== 'ENOENT' || check === root) throw error;
      check = path.dirname(check);
    }
  }
  return target;
}

async function workspace() {
  const files = {};
  let bytes = 0;
  async function walk(dir) {
    for (const entry of await readdir(dir, { withFileTypes: true })) {
      const name = path.join(dir, entry.name);
      const stat = await lstat(name);
      if (stat.isSymbolicLink() || (!stat.isFile() && !stat.isDirectory())) throw new Error('Unsupported workspace file type');
      if (stat.isDirectory()) await walk(name);
      else {
        bytes += stat.size;
        if (bytes > maxBytes || Object.keys(files).length >= 10000) throw new Error('Workspace checkpoint limit exceeded');
        files[path.relative(root, name)] = (await readFile(name)).toString('base64');
      }
    }
  }
  await walk(root);
  return files;
}

async function checkpoint(reason) {
  // All session entries, not just rendered messages: retains compactions and branches.
  const value = { version: 1, pi_version: VERSION, entries: [manager.getHeader(), ...manager.getEntries()],
    leaf_id: manager.getLeafId(), files: await workspace() };
  await request('checkpoint', { reason, checkpoint: value });
}

function wrap(definition) {
  const execute = definition.execute;
  return { ...definition, execute: async (id, input, signal, update, context) => {
    if (stopped) throw new Error('Run stopped');
    if ('path' in input) await safePath(input.path, ['write', 'edit'].includes(definition.name));
    if (['find', 'grep', 'ls'].includes(definition.name)) await safePath(input.path || '.');
    await checkpoint('before_tool');
    const admission = await request('tool_start', { call_id: id, name: definition.name, input });
    if (admission.result) return admission.result;
    if (!admission.allow) throw new Error('Tool execution denied');
    let result;
    try { result = await execute(id, input, signal, update, context); }
    catch (error) { result = { content: [{ type: 'text', text: String(error.message) }], isError: true }; }
    // Receipt + modified files become durable before Pi is allowed to continue.
    await request('tool_end', { call_id: id, name: definition.name, input, result, files: await workspace() });
    return result;
  }};
}

async function start(value) {
  config = value;
  if (VERSION !== '0.85.1' || process.version !== 'v22.22.3') throw new Error('Worker version mismatch');
  for (const [name, content] of Object.entries(config.files || {})) {
    const target = await safePath(name, true);
    await mkdir(path.dirname(target), { recursive: true });
    await writeFile(target, Buffer.from(content, 'base64'));
  }
  manager = SessionManager.inMemory(root, undefined, config.entries);
  if (config.leaf_id) manager.branch(config.leaf_id);
  const server = createServer(async (req, res) => {
    if (req.url !== '/v1/chat/completions' || req.method !== 'POST') { res.writeHead(404).end(); return; }
    try {
      let body = '';
      for await (const chunk of req) {
        body += chunk;
        if (Buffer.byteLength(body) > 4 * 1024 * 1024) throw new Error('Model request limit exceeded');
      }
      const completion = JSON.parse(body);
      if (completion.model !== config.model) throw new Error('Model outside run policy');
      await checkpoint('before_model');
      const id = randomUUID();
      models.set(id, res);
      res.writeHead(200, { 'content-type': 'text/event-stream' });
      res.on('close', () => { if (models.delete(id)) send({ type: 'model_cancel', id }); });
      send({ type: 'model_request', id, request: completion });
    } catch (error) { res.writeHead(400).end(JSON.stringify({ error: { message: error.message } })); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const modelRuntime = await ModelRuntime.create({ modelsPath: null, allowModelNetwork: false,
    refreshOnCreate: false, credentials: { read: async () => undefined, list: async () => [],
      write: async () => { throw new Error('Persistent credentials disabled'); }, delete: async () => {} } });
  modelRuntime.registerProvider('puppyone-cloud', {
    api: 'openai-completions', baseUrl: `http://127.0.0.1:${server.address().port}/v1`, authHeader: true,
    models: [{ id: config.model, name: config.model, reasoning: false, input: ['text'],
      contextWindow: config.context_window, maxTokens: config.max_tokens,
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
      compat: { supportsDeveloperRole: false, supportsStore: false, supportsReasoningEffort: false,
        maxTokensField: 'max_tokens' } }],
  });
  await modelRuntime.setRuntimeApiKey('puppyone-cloud', 'control-channel-only');
  const resourceLoader = {
    getExtensions: () => ({ extensions: [], errors: [], runtime: createExtensionRuntime() }),
    getSkills: () => ({ skills: [], diagnostics: [] }), getPrompts: () => ({ prompts: [], diagnostics: [] }),
    getThemes: () => ({ themes: [], diagnostics: [] }), getAgentsFiles: () => ({ agentsFiles: [] }),
    getSystemPrompt: () => config.system_prompt || 'Help with the assigned project files. Work within this workspace.',
    getSystemPromptSource: () => undefined, getAppendSystemPrompt: () => [], getAppendSystemPromptSources: () => [],
    extendResources: () => {}, reload: async () => {},
  };
  const builtins = [createReadToolDefinition(root), createLsToolDefinition(root), createFindToolDefinition(root), createGrepToolDefinition(root)];
  if (!config.readonly) builtins.push(createWriteToolDefinition(root), createEditToolDefinition(root),
    createBashToolDefinition(root, { exposeSessionEnvironment: false }));
  const custom = (config.tools || []).map(tool => ({ ...tool, label: tool.name,
    execute: async (id, input) => {
      await checkpoint('before_tool');
      const result = await request('bound_tool', { call_id: id, name: tool.name, input });
      if (result.error) throw new Error(result.error);
      return result.result;
    } }));
  ({ session } = await createAgentSession({ cwd: root, agentDir: '/tmp/puppyone-agent', modelRuntime,
    model: modelRuntime.getModel('puppyone-cloud', config.model), thinkingLevel: 'off', sessionManager: manager,
    resourceLoader, tools: [...builtins.map(t => t.name), ...custom.map(t => t.name)],
    customTools: [...builtins.map(wrap), ...custom],
    settingsManager: SettingsManager.inMemory({ defaultProjectTrust: 'never', quietStartup: true,
      retry: { enabled: false }, compaction: { enabled: true, reserveTokens: 4096, keepRecentTokens: 8192 } }),
  }));
  session.subscribe(event => {
    if (event.type === 'message_update' && event.assistantMessageEvent.type === 'text_delta')
      send({ type: 'text', delta: event.assistantMessageEvent.delta });
  });
  session.agent.toolExecution = 'sequential';
  send({ type: 'ready', pi_version: VERSION, node_version: process.version });
  try {
    if (config.resume) {
      // Reconcile completed/approved calls explicitly. Agent.continue() needs
      // a user or tool-result tail; it cannot replay an assistant tool turn.
      const context = manager.buildSessionContext().messages;
      const answered = new Set(context.filter(m => m.role === 'toolResult').map(m => m.toolCallId));
      const definitions = [...builtins.map(wrap), ...custom];
      for (const message of context) {
        if (message.role !== 'assistant') continue;
        for (const call of message.content.filter(part => part.type === 'toolCall')) {
          if (answered.has(call.id)) continue;
          const receipt = (config.receipts || []).find(r => r.call_id === call.id);
          if (!receipt || receipt.state === 'executing') throw new Error('Tool outcome requires reconciliation');
          let result;
          if (receipt.state === 'completed') result = receipt.result.pi_result;
          else if (receipt.state === 'rejected') result = {content: [{type: 'text', text: 'Tool execution denied'}], isError: true};
          else {
            const tool = definitions.find(t => t.name === call.name);
            if (!tool) throw new Error('Tool is no longer available');
            result = await tool.execute(call.id, call.arguments, new AbortController().signal);
          }
          manager.appendMessage({role: 'toolResult', toolCallId: call.id, toolName: call.name,
            content: result.content, details: result.details, isError: Boolean(result.isError), timestamp: Date.now()});
        }
      }
      session.agent.state.messages = manager.buildSessionContext().messages;
      await session.agent.continue();
    }
    else await session.prompt(config.prompt, { expandPromptTemplates: false });
    await checkpoint('settled');
    const last = [...session.agent.state.messages].reverse().find(m => m.role === 'assistant');
    send({ type: 'finished', stopped, error: last?.stopReason === 'error' ? last.errorMessage || 'Model failed' : null });
  } finally {
    session.dispose();
    server.closeAllConnections();
    server.close();
  }
}

const lines = createInterface({ input: process.stdin });
lines.on('line', line => {
  try {
    if (Buffer.byteLength(line) > 96 * 1024 * 1024) throw new Error('Control frame too large');
    const message = JSON.parse(line);
    if (message.type === 'start' && !config) {
      start(message.config).catch(error => send({ type: 'failed', error: error.message }));
    } else if (message.type === 'reply') {
      const waiter = pending.get(message.id);
      pending.delete(message.id);
      if (message.error) waiter?.reject(new Error(message.error));
      else waiter?.resolve(message);
    } else if (message.type === 'model_chunk') {
      models.get(message.id)?.write(`data: ${JSON.stringify(message.frame)}\n\n`);
    } else if (message.type === 'model_end') {
      const res = models.get(message.id);
      models.delete(message.id);
      if (message.error) res?.write(`data: ${JSON.stringify({ error: { message: message.error } })}\n\n`);
      res?.end('data: [DONE]\n\n');
    } else if (message.type === 'stop') {
      stopped = true;
      for (const waiter of pending.values()) waiter.reject(new Error('Run stopped'));
      pending.clear();
      session?.clearQueue();
      session?.abort();
    }
  } catch (error) { send({ type: 'failed', error: error.message }); }
});
lines.on('close', () => { stopped = true; session?.abort(); setTimeout(() => process.exit(1), 1000).unref(); });
