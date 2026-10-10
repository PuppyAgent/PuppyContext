'use client';

import type { WebAgentController, AgentWorkspaceState } from '../runtime/controller';
import { runStatus } from '../runtime/projection';
import { runActive } from '../runtime/types';
import styles from '@/components/chat/AgentChatSurface.module.css';

/** Server facts stay distinct from transport errors and pending submission receipts. */
export function AgentRunNotice({ controller, state }: {
  controller: WebAgentController | null; state: AgentWorkspaceState;
}) {
  const run = state.runs.at(-1);
  const text = runStatus(run);
  const waiting = runActive(run) ? run?.tools?.filter(tool => tool.state === 'waiting') ?? [] : [];
  return <>
    {(state.error || state.pending) && <div className={styles.runNotice} role='alert'>
      <p>{state.pending ? 'Message confirmation is pending. Retry to check the original request.' : state.error?.message}</p>
      <button type='button' className={styles.noticeButton} disabled={state.submitting || state.loading}
        onClick={() => { if (state.pending) void controller?.submit(state.pending.prompt); else void controller?.load(); }}>
        {state.pending ? 'Confirm message' : 'Retry'}
      </button>
      {!state.pending && !runActive(run) && <button type='button' className={styles.noticeButton}
        onClick={() => controller?.newChat()}>New chat</button>}
    </div>}
    {state.reconnecting && <div className={styles.runNotice} role='status'>Reconnecting to Cloud Agent…</div>}
    {text && <div className={styles.runNotice} role='status'>{text}</div>}
    {waiting.map(tool => <div key={tool.call_id} className={styles.runNotice}>
      <p>{tool.name} needs your approval</p>
      <pre>{typeof tool.input.command === 'string' ? tool.input.command : JSON.stringify(tool.input, null, 2)}</pre>
      <button type='button' className={styles.noticeButton} disabled={Boolean(state.resolving)}
        onClick={() => { void controller?.approve(tool.call_id, true); }}>Allow</button>
      <button type='button' className={styles.noticeButton} disabled={Boolean(state.resolving)}
        onClick={() => { void controller?.approve(tool.call_id, false); }}>Decline</button>
    </div>)}
    {state.hasEarlier && <button type='button' className={styles.noticeButton} disabled={state.loading}
      onClick={() => { void controller?.loadEarlier(); }}>Load earlier messages</button>}
  </>;
}
