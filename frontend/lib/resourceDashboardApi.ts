import { createResourceDashboardApi } from '@puppyone/cloud-core';
import { webCloudTransport } from './cloudCoreTransport';
export const { getResourceDashboard } = createResourceDashboardApi(webCloudTransport);
