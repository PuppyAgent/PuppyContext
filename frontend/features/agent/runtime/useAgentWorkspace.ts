'use client';

import { useEffect, useMemo, useRef, useSyncExternalStore } from 'react';
import { useAuth } from '@/contexts/SupabaseAuthProvider';
import { WebAgentClient } from './client';
import { WebAgentController, EMPTY_STATE } from './controller';

const emptySubscribe = () => () => {};
const emptySnapshot = () => EMPTY_STATE;

export function useAgentWorkspace(projectId: string | undefined, agentId: string | null,
  onPublication: () => void, active = true) {
  const { userId, isAuthReady } = useAuth();
  const publication = useRef(onPublication); publication.current = onPublication;
  const key = JSON.stringify(['web-cloud-agent-v1', userId, projectId, agentId]);
  const controller = useMemo(() => isAuthReady && userId && projectId && agentId
    ? new WebAgentController(new WebAgentClient(projectId, agentId), key) : null,
  [isAuthReady, userId, projectId, agentId, key]);
  const state = useSyncExternalStore(controller?.subscribe ?? emptySubscribe,
    controller?.getSnapshot ?? emptySnapshot, emptySnapshot);
  useEffect(() => active ? controller?.observe(() => publication.current()) : undefined, [controller, active]);
  return { controller, state };
}
