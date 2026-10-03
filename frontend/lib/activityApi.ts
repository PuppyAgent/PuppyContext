/** Read-only typed activity; lifecycle remains with each owning entrypoint. */
import { createActivityApi } from '@puppyone/cloud-core';
import { webCloudTransport } from './cloudCoreTransport';

export type { ActivityKind, ActivityItem, ActivityListResponse, ActivitySelectors } from '@puppyone/cloud-core';
export { isActivityItemActive } from '@puppyone/cloud-core';
export const { getProjectActivity } = createActivityApi(webCloudTransport);
