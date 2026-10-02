'use client';

import { CommandBlock } from '@/features/files/components/access-points/connect-methods/CommandBlock';
import { regenerateAccessSurfaceKey } from '@/lib/repoApi';
import { useAuth } from '@/contexts/SupabaseAuthProvider';
import { repositoryTargetKey, sameRepositoryTarget, type RepositoryTarget } from '@puppyone/cloud-core';
import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';

const CLI_CREDENTIAL_PATTERN = /^cli_[A-Za-z0-9_-]{32,128}$/;

function acceptedCliCredential(value: string | null | undefined): string | null {
  const candidate = value?.trim() || '';
  return CLI_CREDENTIAL_PATTERN.test(candidate) ? candidate : null;
}

export function CliCredentialIssuePanel({
  connectorId,
  target,
  children,
}: {
  readonly connectorId: string;
  readonly target: RepositoryTarget;
  readonly children?: (credential: string) => ReactNode;
}) {
  const { userId, session, isAuthReady } = useAuth();
  const contextKey = JSON.stringify([userId, connectorId, repositoryTargetKey(target)]);
  const generation = useRef(0);
  const [view, setView] = useState<{
    contextKey: string; credential: string | null; issuing: boolean; error: string | null;
  }>({ contextKey, credential: null, issuing: false, error: null });
  const current = view.contextKey === contextKey && isAuthReady && userId;
  const issuedCredential = current ? view.credential : null;
  const issuing = current ? view.issuing : false;
  const error = current ? view.error : null;

  useEffect(() => {
    ++generation.current;
    setView({ contextKey, credential: null, issuing: false, error: null });
    return () => { ++generation.current; };
  }, [contextKey, session?.access_token, isAuthReady]);

  const issue = useCallback(async () => {
    if (!connectorId || issuing || !userId || !isAuthReady) return;
    const requestGeneration = generation.current;
    setView({ contextKey, credential: null, issuing: true, error: null });
    try {
      const result = await regenerateAccessSurfaceKey(connectorId);
      if (requestGeneration !== generation.current) return;
      const credential = acceptedCliCredential(result.credential);
      if (result.access_surface_id !== connectorId || !result.target || !sameRepositoryTarget(result.target, target) || !credential) {
        throw new Error('Cloud returned an invalid one-time CLI credential');
      }
      setView({ contextKey, credential, issuing: false, error: null });
    } catch (caught) {
      if (requestGeneration !== generation.current) return;
      setView({ contextKey, credential: null, issuing: false,
        error: caught instanceof Error ? caught.message : 'Unable to generate CLI key' });
    }
  }, [connectorId, contextKey, isAuthReady, issuing, target, userId]);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      <div style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 8 }}>
        <button
          type='button'
          onClick={() => void issue()}
          disabled={!connectorId || issuing || !userId || !isAuthReady}
          style={{
            minHeight: 30,
            padding: '0 10px',
            border: '1px solid var(--po-border)',
            borderRadius: 6,
            background: 'var(--po-panel)',
            color: 'var(--po-text)',
            cursor: !connectorId || issuing || !userId || !isAuthReady ? 'not-allowed' : 'pointer',
            fontSize: 12,
          }}
        >
          {issuing ? 'Generating…' : issuedCredential ? 'Rotate CLI key' : 'Generate new CLI key'}
        </button>
        <span style={{ color: 'var(--po-text-subtle)', fontSize: 11 }}>
          Generating a key revokes the previous CLI key and legacy key-in-URL access.
        </span>
      </div>

      {issuedCredential ? (
        <>
          <CommandBlock lines={[`CLI key: ${issuedCredential}`]} />
          <span style={{ color: 'var(--po-text-subtle)', fontSize: 11 }}>
            Save this key now. It is shown only once, kept only in this page, and is separate from Git credentials.
          </span>
          {children?.(issuedCredential)}
        </>
      ) : (
        <span style={{ color: 'var(--po-text-subtle)', fontSize: 11 }}>
          Existing keys cannot be recovered from ordinary reads. Generate a new one when you need CLI setup.
        </span>
      )}

      {error ? <span style={{ color: 'var(--po-danger)', fontSize: 11 }}>{error}</span> : null}
    </div>
  );
}
