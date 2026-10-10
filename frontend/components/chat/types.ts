/** Presentation types shared by chat chrome and message components. */

export interface ChatSession {
  id: string;
  agent_id: string | null;
  title: string | null;
  mode: string | null;
  created_at: string;
  updated_at: string;
}

export interface MessagePart {
  type: 'text' | 'tool';
  content?: string;
  toolId?: string;
  toolName?: string;
  toolInput?: string;
  toolOutput?: string;
  toolStatus?: 'running' | 'completed' | 'error';
}
