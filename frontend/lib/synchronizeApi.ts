import { createSynchronizeApi } from '@puppyone/cloud-core';
import { webCloudTransport } from './cloudCoreTransport';

export type {
  SynchronizeBinding, SynchronizeBindingCreate, SynchronizeBindingCreated,
  SynchronizeBindingUpdate, SynchronizeTriggerUpdate, SynchronizeRun,
  SynchronizeExecutionResult, SynchronizeFailedRun, SynchronizeStatus,
  SynchronizeStatusItem, SynchronizePullResult, SynchronizePushResult,
  SynchronizeProviderSpec, SynchronizeConfigField, SynchronizeSourceResource,
  SynchronizeMaterializationSchema, SynchronizeProviderResources,
} from '@puppyone/cloud-core';

export const {
  listSynchronizeProviders, listSynchronizeProviderResources,
  createSynchronizeBinding, listSynchronizeBindings, updateSynchronizeBinding,
  deleteSynchronizeBinding, updateSynchronizeTrigger, pauseSynchronizeBinding,
  resumeSynchronizeBinding, refreshSynchronizeBinding, listSynchronizeRuns,
  getSynchronizeRun, listFailedSynchronizeRuns, getSynchronizeStatus,
  bootstrapSynchronizeBindings, pullSynchronizeBindings, pushSynchronizePath,
} = createSynchronizeApi(webCloudTransport);
