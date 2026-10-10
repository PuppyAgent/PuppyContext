import type { Submission } from './types';

export type SavedConversation = { sessionId: string | null; pending: Submission | null; newChat: boolean; draft: string };
export const EMPTY_SAVED: SavedConversation = { sessionId: null, pending: null, newChat: false, draft: '' };
export function readConversation(key: string, agent: string, initial = EMPTY_SAVED): SavedConversation {
  try {
    const raw = sessionStorage.getItem(key);
    if (!raw) return { ...initial };
    const saved = JSON.parse(raw);
    const value = saved.pending;
    const pending: Submission | null = value && value.agent_id === agent &&
      typeof value.prompt === 'string' && typeof value.request_id === 'string' &&
      /^[\da-f]{8}(-[\da-f]{4}){3}-[\da-f]{12}$/i.test(value.request_id) &&
      (value.session_id === undefined || typeof value.session_id === 'string') ? value : null;
    return { sessionId: typeof saved.sessionId === 'string' ? saved.sessionId : null,
      pending, newChat: saved.newChat === true, draft: typeof saved.draft === 'string' ? saved.draft : pending?.prompt ?? '' };
  } catch { return { ...initial }; }
}
export function saveConversation(key: string, value: SavedConversation) {
  try { sessionStorage.setItem(key, JSON.stringify(value)); } catch { /* memory receipt still works */ }
}
