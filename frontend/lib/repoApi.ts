/** Web transport binding for Scopes, Access surfaces and repository identity. */
import { createScopesApi, createAccessSurfacesApi, createRepositoryIdentityApi } from '@puppyone/cloud-core';
import { webCloudTransport } from './cloudCoreTransport';

export { isGitRemoteProvider, normalizeConnectorProvider } from '@/lib/accessProviderRegistry';
export {
  matchScopeForPath,
  matchRepositoryViewForPath,
  projectRootRepositoryView,
  repositoryScopeView,
  repositoryTargetKey,
  repositoryViewKey,
  isWithinScope,
  BUILTIN_ACCESS_KINDS,
  isAccessSurface,
  filterAccessSurfaces,
  sortAccessSurfacesBuiltinFirst,
} from '@puppyone/cloud-core';
export type {
  ScopeMode,
  RepositoryScope,
  RepositoryTarget,
  RepositoryView,
  AccessDirection,
  AccessSurfaceStatus,
  AccessSurface,
  AccessSurfaceCreate,
  AccessSurfaceUpdate,
  RepoIdentity,
} from '@puppyone/cloud-core';

const scopesApi = createScopesApi(webCloudTransport);
const accessApi = createAccessSurfacesApi(webCloudTransport);
const identityApi = createRepositoryIdentityApi(webCloudTransport);

export const listScopes = scopesApi.listScopes;
export const createScope = scopesApi.createScope;
export const updateScope = scopesApi.updateScope;
export const deleteScope = scopesApi.deleteScope;

export const listAccessSurfaces = accessApi.listAccessSurfaces;
export const createAccessSurface = accessApi.createAccessSurface;
export const enableTargetAccess = accessApi.enableTargetAccess;
export const updateAccessSurface = accessApi.updateAccessSurface;
export const deleteAccessSurface = accessApi.deleteAccessSurface;
export const activateAccessSurfaceAgent = accessApi.activateAccessSurfaceAgent;
export const pauseAccessSurface = accessApi.pauseAccessSurface;
export const resumeAccessSurface = accessApi.resumeAccessSurface;
export const regenerateAccessSurfaceKey = accessApi.regenerateAccessSurfaceKey;
export const getRepoIdentity = identityApi.getRepoIdentity;
