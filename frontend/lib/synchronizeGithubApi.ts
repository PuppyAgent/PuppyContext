import { createSynchronizeGithubApi, synchronizeGithubWebhookUrl } from '@puppyone/cloud-core';
import { webCloudTransport } from './cloudCoreTransport';
export type {
  SynchronizeGithubDirection, SynchronizeGithubStatus, SynchronizeGithubBindingCreate,
  SynchronizeGithubBindingUpdate, SynchronizeGithubBinding, SynchronizeGithubRepo,
  SynchronizeGithubRepos, SynchronizeGithubBranch, SynchronizeGithubBranches,
  SynchronizeGithubPull, SynchronizeGithubPush, SynchronizeGithubResult,
  SynchronizeGithubLog, SynchronizeGithubLogs,
} from '@puppyone/cloud-core';
export const {
  getSynchronizeGithubBinding, createSynchronizeGithubBinding, updateSynchronizeGithubBinding,
  deleteSynchronizeGithubBinding, listSynchronizeGithubRepos, listSynchronizeGithubBranches,
  pullSynchronizeGithubBinding, pushSynchronizeGithubBinding, listSynchronizeGithubLogs,
} = createSynchronizeGithubApi(webCloudTransport);
export function githubSynchronizeWebhookUrl(): string {
  return synchronizeGithubWebhookUrl(process.env.NEXT_PUBLIC_API_URL || 'http://localhost:9090');
}
