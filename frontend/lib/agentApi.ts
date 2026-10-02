import { pauseAccessSurface, resumeAccessSurface } from './repoApi';

/** Agent configurations project the same persisted kind='agent' Access ID. */
export async function pauseAgent(projectId: string, agentId: string): Promise<void> {
  await pauseAccessSurface(projectId, agentId);
}

export async function resumeAgent(projectId: string, agentId: string): Promise<void> {
  await resumeAccessSurface(projectId, agentId);
}
