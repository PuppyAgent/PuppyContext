/** Trusted sandbox control process. Pi and every model tool run as uid 1000. */
import { spawn, execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { createInterface } from 'node:readline';
import { mkdir, chmod, chown, readdir, readFile } from 'node:fs/promises';
import { randomUUID } from 'node:crypto';
import { channel } from './protocol.mjs';
import { ROOT, prepare, finalize, push, exists, inspect } from './git.mjs';
import { gitRelay } from './git-relay.mjs';
const exec = promisify(execFile);
const control = channel();
let worker, relay, config, lastSnapshot, stopped = false;
const childIds = new Set();
async function processes(signal, keepWorker = false) {
  // Include detached descendants, not just the original process group.
  for (let attempt=0; attempt<8; attempt++) {
    let found = false;
    for (const name of await readdir('/proc')) {
      if (!/^\d+$/.test(name)) continue;
      if (keepWorker && Number(name) === worker?.pid) continue;
      try {
        const status = await readFile(`/proc/${name}/status`,'utf8');
        if (!/^Uid:\s+1000\s/m.test(status) || /^State:\s+[Zt]\b/m.test(status)) continue;
        if (signal === 'SIGSTOP' && /^State:\s+T\b/m.test(status)) continue;
        process.kill(Number(name), signal); found = true;
      } catch (error) { if (!['ENOENT','ESRCH'].includes(error.code)) throw error; }
    }
    if (!found || signal === 'SIGCONT') return;
    await new Promise(resolve=>setTimeout(resolve,20));
  }
  if (signal === 'SIGSTOP') throw new Error('Could not freeze workspace writers');
}
async function directories() {
  await mkdir(ROOT,{recursive:true}); await chown(ROOT,1000,1000);
  await mkdir('/recovery',{recursive:true}); await chmod('/recovery',0o700);
}
async function snapshot() {
  await processes('SIGSTOP');
  try {
    // A completed tool may have forked detached writers. They must not modify
    // files after its acknowledged boundary or during subsequent read tools.
    await processes('SIGKILL',true);
    const id=randomUUID(); const destination=`/recovery/${id}`;
    await mkdir(destination);
    await exec('rsync',['-a',...(lastSnapshot ? [`--link-dest=/recovery/${lastSnapshot}`] : []),
      '--',ROOT+'/',destination+'/'],{timeout:120000,maxBuffer:8192});
    lastSnapshot=id;
    return { id };
  } finally { if (!stopped) await processes('SIGCONT'); }
}
async function prepareWorkspace(value) {
  config=value; stopped=false; await directories();
  relay?.close(); relay=await gitRelay(control.request,config.project_id);
  if (config.restore) {
    if (!/^[0-9a-f-]{36}$/.test(config.restore)) throw new Error('Invalid recovery identity');
    if (!(await exists(`/recovery/${config.restore}`))) throw new Error('Recovery point missing');
    await exec('rsync',['-a','--delete','--',`/recovery/${config.restore}/`,ROOT+'/'],{timeout:120000});
    lastSnapshot=config.restore;
  }
  if (config.resume) return {};
  return relay.run(()=>prepare(ROOT,config,relay.url,{reuse:Boolean(config.restore)}));
}
function start(value) {
  if (worker) throw new Error('Worker already started');
  worker=spawn('node',['/opt/puppyone-agent/worker.mjs'],{cwd:ROOT,uid:1000,gid:1000,
    env:{PATH:'/usr/local/bin:/usr/bin:/bin',HOME:'/home/node',NODE_ENV:'production'},stdio:['pipe','pipe','pipe']});
  worker.stderr.on('data',()=>{});
  createInterface({input:worker.stdout}).on('line',line=>{
    try {
      const frame=JSON.parse(line);
      if (frame.id) childIds.add(frame.id);
      // The model process cannot forge trusted Git/provider control frames.
      if (!['checkpoint','tool_start','tool_end','bound_tool','model_request','model_cancel','text','finished','failed','ready'].includes(frame.type))
        throw new Error('Invalid worker message');
      control.send(frame);
    } catch (error) { control.send({type:'failed',error:error.message}); }
  });
  worker.on('exit',()=>{ if (!stopped) control.send({type:'disconnected'}); });
  worker.stdin.write(JSON.stringify({type:'start',config:value})+'\n');
}
control.dispatch(async frame=>{
  if (frame.type === 'control') {
    try {
      let result;
      if (frame.action === 'prepare') result=await prepareWorkspace(frame.value);
      else if (frame.action === 'snapshot') result=await snapshot();
      else if (frame.action === 'inspect') result=await inspect(ROOT,frame.value);
      else if (frame.action === 'freeze') {
        await processes('SIGSTOP'); await processes('SIGKILL',true); result={};
      }
      else if (frame.action === 'thaw') { if (!stopped) await processes('SIGCONT'); result={}; }
      else if (frame.action === 'finalize') {
        stopped=true; await processes('SIGKILL'); worker=null;
        result=await finalize(ROOT,config,frame.value.message);
      } else if (frame.action === 'push') {
        if (!stopped) throw new Error('Worker must stop before publication');
        result=await relay.run(()=>push(ROOT,config,relay.url,frame.value.candidate));
      } else if (frame.action === 'stop') { stopped=true; await processes('SIGKILL'); result={}; }
      else throw new Error('Unknown control operation');
      control.send({type:'control_result',id:frame.id,result});
    } catch (error) { control.send({type:'control_result',id:frame.id,error:error.message}); }
  } else if (frame.type === 'start') start(frame.config);
  else if (worker && (frame.type !== 'reply' || childIds.has(frame.id))) {
    childIds.delete(frame.id); worker.stdin.write(JSON.stringify(frame)+'\n');
  }
});
process.stdin.on('end',()=>{ stopped=true; processes('SIGKILL').finally(()=>process.exit(0)); });
