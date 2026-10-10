/** Browser-only ports. The production workbench renders against fake Cloud runs. */
export const useAuth = () => ({ userId: 'browser-fixture', isAuthReady: true });
export const useAgent = () => ({ currentAgentId: 'agent-1', savedAgents: [] });
export const useOnboarding = () => ({ completeStep: () => undefined });
export const refreshAllContentNodes = () => undefined;
export const refreshProjectHistory = () => undefined;
export const apiRequest = () => { throw new Error('Unexpected network request in browser fixture'); };
export const apiStreamRequest = apiRequest;
