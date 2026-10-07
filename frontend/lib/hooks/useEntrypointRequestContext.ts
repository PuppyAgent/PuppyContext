import { useCallback, useEffect, useMemo, useRef } from 'react';
import { useAuth } from '@/contexts/SupabaseAuthProvider';

/** Reject late UI effects after account/session/Project/resource or lifetime changes.
 * This does not cancel an already authorized server mutation or undo its result.
 */
export function useEntrypointRequestContext(projectId: string, resourceId?: string) {
  const { userId, session, isAuthReady } = useAuth();
  const context = useMemo(() => ({ projectId, resourceId, userId, token: session?.access_token, isAuthReady }), [projectId, resourceId, userId, session?.access_token, isAuthReady]);
  const current = useRef(context);
  const mounted = useRef(false);
  current.current = context;
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  return useCallback(() => {
    const captured = context;
    return () => mounted.current && current.current === captured && isAuthReady && Boolean(userId);
  }, [context, isAuthReady, userId]);
}
