'use client';

import { useRef, useState } from 'react';
import { ArrowLeft, History, LoaderCircle, RefreshCw, Search } from 'lucide-react';
import { AgentChatBrand } from '@/components/chat/AgentChatChrome';
import type { AgentWorkbenchController, WorkbenchState } from './controller';
import styles from './workbench.module.css';

/** Same full-pane locator browser as Desktop; no transcript is fetched here. */
export function AgentHistoryBrowser({ store, state, launcherId, onBack }: {
  store: AgentWorkbenchController; state: WorkbenchState; launcherId?: string; onBack: () => void;
}) {
  const [searchOpen, setSearchOpen] = useState(false);
  const [query, setQuery] = useState('');
  const searchButton = useRef<HTMLButtonElement>(null);
  const open = new Set(state.tabs.flatMap(tab => tab.kind === 'chat' ? [store.actor(tab).getSnapshot().sessionId] : []));
  const available = state.sessions.filter(session => state.agents.some(agent => agent.id === session.agent_id));
  const sessions = available.filter(session => !open.has(session.id));
  const matching = sessions.filter(session => `${session.title ?? ''}\n${state.agents.find(agent => agent.id === session.agent_id)?.name ?? ''}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()));
  let empty = 'No chat history yet';
  if (available.length) empty = 'Your conversations are already open.';
  if (query) empty = 'No matching conversations';
  if (state.historyError) empty = 'Could not load chat history. Try refreshing.';
  const loading = state.historyLoading && !available.length;
  return <section className={styles.history} aria-label='Chat history' onKeyDown={event => {
    if (event.key !== 'Escape') return;
    event.preventDefault(); event.stopPropagation();
    if (searchOpen) { setQuery(''); setSearchOpen(false); requestAnimationFrame(() => searchButton.current?.focus()); }
    else onBack();
  }}>
    <header className={styles.historyToolbar}>
      <button type='button' className={styles.historyButton} title='Back' aria-label='Back to new tab' onClick={onBack}><ArrowLeft size={15} strokeWidth={1.7} /></button>
      <div className={styles.searchSlot}>
        <button ref={searchButton} type='button' className={styles.historyButton} hidden={searchOpen} title='Search chats' aria-label='Search chats' aria-expanded={searchOpen} onClick={() => setSearchOpen(true)}><Search size={15} strokeWidth={1.7} /></button>
        {searchOpen && <label className={styles.search}><Search size={13} strokeWidth={1.7} /><input type='search' autoFocus aria-label='Search chats' placeholder='Search chats' value={query} onChange={event => setQuery(event.target.value)} /></label>}
      </div>
      <button type='button' className={styles.historyButton} title='Refresh history' aria-label='Refresh history' disabled={state.historyLoading} onClick={() => void store.refreshHistory()}><RefreshCw size={12} strokeWidth={1.7} className={state.historyLoading ? styles.spin : undefined} /></button>
    </header>
    {loading && <div className={styles.historyEmpty} role='status'><LoaderCircle size={13} className={styles.spin} />Loading history…</div>}
    {!loading && !matching.length && <div className={styles.historyEmpty} role={state.historyError ? 'alert' : 'status'}><History size={13} />{empty}</div>}
    {!loading && matching.length > 0 && <ul className={styles.historyList}>{matching.map(session => <li key={session.id}>
          <button type='button' className={styles.historyOption} aria-label={`Open ${session.title || 'New chat'}`} onClick={() => store.openSession(session, launcherId)}>
            <span className={styles.monochrome}><AgentChatBrand /></span><span className={styles.historyTitle}>{session.title || 'New chat'}</span>
            <time className={styles.historyTime} dateTime={session.updated_at}>{formatDate(session.updated_at)}</time>
          </button>
        </li>)}</ul>}
    <footer className={styles.historyFooter}>
      {state.historyError && matching.length > 0 && <p role='status'>Could not refresh history. Try again.</p>}
      {state.historyLimited && <p>Showing the latest 200 conversations per agent.</p>}
    </footer>
  </section>;
}

function formatDate(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? '' : new Intl.DateTimeFormat(undefined, {
    month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
  }).format(date);
}
