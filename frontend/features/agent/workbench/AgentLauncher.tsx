'use client';

import { History } from 'lucide-react';
import { AgentChatBrand } from '@/components/chat/AgentChatChrome';
import type { AgentWorkbenchController, WorkbenchState } from './controller';
import styles from './workbench.module.css';

export function AgentLauncher({ store, state, tab, onConfigure }: {
  store: AgentWorkbenchController; state: WorkbenchState; tab?: string; onConfigure?: () => void;
}) {
  return <section className={styles.launcher} aria-label='New tab'>
    <div className={styles.launcherContent}><div className={styles.launcherGroup}>
      <div className={styles.launcherHeading}><h2>Chat</h2></div>
      <button type='button' className={styles.launcherHistory} aria-label='Chat history' title='Chat history' onClick={() => store.showHistory(tab)}><History size={14} /></button>
      {state.agents.map(agent => <button key={agent.id} type='button' className={styles.launcherOption} onClick={() => store.newChat(agent.id, tab)}><AgentChatBrand /><span>{agent.name}</span></button>)}
      {!state.agents.length && <button type='button' className={styles.launcherOption} disabled={!onConfigure} onClick={onConfigure}><AgentChatBrand /><span>Set up a chat agent</span></button>}
    </div></div>
  </section>;
}
