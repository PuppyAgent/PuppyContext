import { createImportDatabaseApi } from '@puppyone/cloud-core';
import { webCloudTransport } from './cloudCoreTransport';
export type {
  ImportDatabaseSource, ImportDatabaseSourceCreate, ImportDatabaseSourceCreated,
  ImportDatabaseKeyType, ImportDatabaseTable, ImportDatabasePreview, ImportDatabaseSave,
  ImportDatabaseSaved, ImportDatabaseErrorDetail,
} from '@puppyone/cloud-core';
export const {
  createImportDatabaseSource, listImportDatabaseSources, getImportDatabaseSource, deleteImportDatabaseSource,
  listImportDatabaseTables, previewImportDatabaseTable, saveImportDatabaseTable,
} = createImportDatabaseApi(webCloudTransport);
