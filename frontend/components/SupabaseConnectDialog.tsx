'use client';

import { useState } from 'react';
import { createImportDatabaseSource, type ImportDatabaseErrorDetail, type ImportDatabaseKeyType } from '../lib/importDatabaseApi';
import { Dots } from './loading';
import { ActivityIconButton } from './ActivityIconButton';
import { ActionButton } from './ui/ActionButton';

type SupabaseConnectDialogProps = {
  projectId: string;
  onClose: () => void;
  onConnected: (importDatabaseSourceId: string) => void;
};

export function SupabaseConnectDialog({ projectId, onClose, onConnected }: SupabaseConnectDialogProps) {
  const [projectUrl, setProjectUrl] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [keyType, setKeyType] = useState<ImportDatabaseKeyType>('anon'); // Default to anon
  const [isConnecting, setIsConnecting] = useState(false);
  const [error, setError] = useState<ImportDatabaseErrorDetail | null>(null);
  const [showRLSGuide, setShowRLSGuide] = useState(false);

  const canSubmit = projectUrl.trim().length > 0 && apiKey.trim().length > 0;

  const handleConnect = async () => {
    if (!canSubmit) return;
    setIsConnecting(true);
    setError(null);

    try {
      const urlHost = new URL(projectUrl.trim()).hostname;
      const ref = urlHost.split('.')[0];
      const name = `Supabase (${ref})`;

      const { source } = await createImportDatabaseSource(projectId, {
        name,
        provider: 'supabase',
        project_url: projectUrl.trim(),
        api_key: apiKey.trim(),
        key_type: keyType,
      });

      onConnected(source.id);
    } catch (err: unknown) {
      const errorDetail: ImportDatabaseErrorDetail = err instanceof Error
        ? {
            error_code: null,
            message: err.message,
            suggested_actions: [],
          }
        : (err as ImportDatabaseErrorDetail);

      if (errorDetail.error_code === 'RLS_BLOCKED') {
        // Show RLS guidance
        setShowRLSGuide(true);
      } else {
        setError(errorDetail);
      }
    } finally {
      setIsConnecting(false);
    }
  };

  return (
    <div
      style={{
        position: 'fixed', inset: 0, background: 'var(--po-backdrop)',
        display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000,
        backdropFilter: 'blur(2px)',
      }}
      onClick={onClose}
    >
      <div
        style={{
          background: 'var(--po-overlay)',
          border: '1px solid var(--po-border)',
          borderRadius: 12,
          width: 520,
          maxWidth: '90vw',
          boxShadow: '0 24px 48px var(--po-shadow)',
          display: 'flex', flexDirection: 'column', overflow: 'hidden',
          animation: 'dialog-fade-in 0.2s ease-out',
        }}
        onClick={e => e.stopPropagation()}
      >
        <style jsx>{`
          @keyframes dialog-fade-in {
            from { opacity: 0; transform: scale(0.98); }
            to { opacity: 1; transform: scale(1); }
          }
          input::placeholder { color: var(--po-text-disabled); }
        `}</style>

        {/* Header */}
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '14px 20px 8px' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <svg width="20" height="20" viewBox="0 0 109 113" fill="none">
              <path d="M63.7076 110.284C60.8481 113.885 55.0502 111.912 54.9813 107.314L53.9738 40.0627L99.1935 40.0627C107.384 40.0627 111.952 49.5228 106.859 55.9374L63.7076 110.284Z" fill="url(#sp0)"/>
              <path d="M63.7076 110.284C60.8481 113.885 55.0502 111.912 54.9813 107.314L53.9738 40.0627L99.1935 40.0627C107.384 40.0627 111.952 49.5228 106.859 55.9374L63.7076 110.284Z" fill="url(#sp1)" fillOpacity="0.2"/>
              <path d="M45.317 2.07103C48.1765 -1.53037 53.9745 0.442937 54.0434 5.041L54.4849 72.2922H9.83113C1.64038 72.2922 -2.92775 62.8321 2.1655 56.4175L45.317 2.07103Z" fill="#3ECF8E"/>
              <defs>
                <linearGradient id="sp0" x1="53.9738" y1="54.974" x2="94.1635" y2="71.8295" gradientUnits="userSpaceOnUse"><stop stopColor="#249361"/><stop offset="1" stopColor="#3ECF8E"/></linearGradient>
                <linearGradient id="sp1" x1="36.1558" y1="30.578" x2="54.4844" y2="65.0806" gradientUnits="userSpaceOnUse"><stop/><stop offset="1" stopOpacity="0"/></linearGradient>
              </defs>
            </svg>
            <span style={{ color: 'var(--po-text-muted)', fontSize: 13, fontWeight: 500 }}>Connect Supabase</span>
          </div>
          <ActivityIconButton kind="close" title="Close" onClick={onClose} />
        </div>

        {/* Body */}
        <div style={{ padding: '20px', display: 'flex', flexDirection: 'column', gap: 16 }}>
          {/* Key Type Selector */}
          <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            <div style={{ fontSize: 13, color: 'var(--po-text-muted)', fontWeight: 500, marginBottom: 8 }}>
              Select API Key Type
            </div>
            <div style={{ display: 'flex', gap: 12 }}>
              {/* Anon Key Option */}
              <label
                style={{
                  display: 'flex',
                  flex: 1,
                  padding: '12px',
                  border: `2px solid ${keyType === 'anon' ? 'color-mix(in srgb, #3ECF8E 50%, transparent)' : 'var(--po-active)'}`,
                  borderRadius: 8,
                  cursor: 'pointer',
                  transition: 'all 0.2s',
                }}
                onClick={() => setKeyType('anon')}
              >
                <input
                  type="radio"
                  name="keyType"
                  checked={keyType === 'anon'}
                  onChange={() => setKeyType('anon')}
                  style={{ margin: 0 }}
                />
                <div style={{ marginLeft: 8 }}>
                  <div style={{ fontSize: 14, fontWeight: 500, color: 'var(--po-text)' }}>Anon Key</div>
                  <div style={{ fontSize: 12, color: 'var(--po-text-muted)' }}>Recommended - Secure, public access</div>
                </div>
              </label>

              {/* Service Role Key Option */}
              <label
                style={{
                  display: 'flex',
                  flex: 1,
                  padding: '12px',
                  border: `2px solid ${keyType === 'service_role' ? 'color-mix(in srgb, #3ECF8E 50%, transparent)' : 'var(--po-active)'}`,
                  borderRadius: 8,
                  cursor: 'pointer',
                  transition: 'all 0.2s',
                }}
                onClick={() => setKeyType('service_role')}
              >
                <input
                  type="radio"
                  name="keyType"
                  checked={keyType === 'service_role'}
                  onChange={() => setKeyType('service_role')}
                  style={{ margin: 0 }}
                />
                <div style={{ marginLeft: 8 }}>
                  <div style={{ fontSize: 14, fontWeight: 500, color: 'var(--po-text)' }}>Service Role Key</div>
                  <div style={{ fontSize: 12, color: 'var(--po-text-muted)' }}>⚠️ Full database access - Use with caution</div>
                </div>
              </label>
            </div>
          </div>

          {/* Project URL */}
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            <label style={{ fontSize: 13, color: 'var(--po-text-muted)', fontWeight: 500 }}>Project URL</label>
            <input
              type="text"
              placeholder="https://your-project.supabase.co"
              value={projectUrl}
              onChange={e => { setProjectUrl(e.target.value); setError(null); }}
              style={{
                background: 'var(--po-hover)', border: '1px solid var(--po-active)',
                borderRadius: 8, padding: '10px 12px', color: 'var(--po-text)', fontSize: 14, outline: 'none',
              }}
              onFocus={e => e.target.style.borderColor = 'color-mix(in srgb, #3ECF8E 50%, transparent)'}
              onBlur={e => e.target.style.borderColor = 'var(--po-active)'}
              autoFocus
            />
            <span style={{ fontSize: 12, color: 'var(--po-text-disabled)' }}>Settings → API → Project URL</span>
          </div>

          {/* API Key Input */}
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            <label style={{ fontSize: 13, color: 'var(--po-text-muted)', fontWeight: 500 }}>API Key</label>
            <input
              type="password"
              placeholder="eyJhbGciOiJIUzI1NiIs..."
              value={apiKey}
              onChange={e => { setApiKey(e.target.value); setError(null); }}
              onKeyDown={e => { if (e.key === 'Enter' && canSubmit) handleConnect(); }}
              style={{
                background: 'var(--po-hover)', border: '1px solid var(--po-active)',
                borderRadius: 8, padding: '10px 12px', color: 'var(--po-text)', fontSize: 14, outline: 'none',
                fontFamily: 'var(--po-font-sans)',
              }}
              onFocus={e => e.target.style.borderColor = 'color-mix(in srgb, #3ECF8E 50%, transparent)'}
              onBlur={e => e.target.style.borderColor = 'var(--po-active)'}
            />
            <span style={{ fontSize: 12, color: 'var(--po-text-disabled)' }}>
              {keyType === 'anon'
                ? 'Settings → API → Project API keys → anon public'
                : 'Settings → API → Project API keys → service_role'}
            </span>
          </div>

          {/* Security Warning for service_role */}
          {keyType === 'service_role' && (
            <div style={{
              padding: '12px',
              background: 'color-mix(in srgb, var(--po-danger) 10%, transparent)',
              border: '1px solid color-mix(in srgb, var(--po-danger) 20%, transparent)',
              borderRadius: 8,
              display: 'flex',
              gap: 8,
              alignItems: 'flex-start',
            }}>
              <span style={{ fontSize: 20 }}>⚠️</span>
              <div style={{ flex: 1 }}>
                <div style={{ fontSize: 13, fontWeight: 600, color: 'var(--po-danger)', marginBottom: 4 }}>
                  Security Warning
                </div>
                <div style={{ fontSize: 12, color: 'var(--po-text)', lineHeight: 1.5 }}>
                  The <strong>Service Role Key</strong> has full permissions to read, write, and delete data in your database.
                  Only use this for data sources you trust.
                </div>
              </div>
            </div>
          )}

          {/* Info Box for anon */}
          {keyType === 'anon' && (
            <div style={{
              padding: '12px',
              background: 'color-mix(in srgb, var(--po-accent) 10%, transparent)',
              border: '1px solid color-mix(in srgb, var(--po-accent) 20%, transparent)',
              borderRadius: 8,
              display: 'flex',
              gap: 8,
              alignItems: 'flex-start',
            }}>
              <span style={{ fontSize: 20 }}>ℹ️</span>
              <div style={{ flex: 1 }}>
                <div style={{ fontSize: 12, color: 'var(--po-accent)', marginBottom: 4 }}>
                  Row Level Security (RLS)
                </div>
                <div style={{ fontSize: 12, color: 'var(--po-text-muted)', lineHeight: 1.5 }}>
                  Your Supabase may have Row Level Security (RLS) enabled.
                  {'If you get a "403 Access Denied" error, we\'ll guide you through RLS configuration.'}
                </div>
              </div>
            </div>
          )}

          {/* Error */}
          {error && (
            <div style={{
              padding: '12px',
              background: 'color-mix(in srgb, var(--po-danger) 10%, transparent)',
              border: '1px solid color-mix(in srgb, var(--po-danger) 20%, transparent)',
              borderRadius: 8,
            }}>
              <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8, marginBottom: error.error_code === 'RLS_BLOCKED' ? 12 : 0 }}>
                <span style={{ fontSize: 16, color: 'var(--po-danger)' }}>
                  {error.error_code === 'RLS_BLOCKED' ? '⚠️' : '⚠️'}
                </span>
                <div style={{ flex: 1 }}>
                  <div style={{ fontSize: 13, fontWeight: 600, color: 'var(--po-danger)', marginBottom: 4 }}>
                    {error.error_code === 'RLS_BLOCKED' ? 'Row Level Security Detected' : 'Access Setup Failed'}
                  </div>
                  <div style={{ fontSize: 12, color: 'var(--po-text)', lineHeight: 1.5 }}>
                    {error.message}
                  </div>
                  {error.suggested_actions && error.suggested_actions.length > 0 && (
                    <div style={{ marginTop: 12 }}>
                      <div style={{ fontSize: 12, fontWeight: 500, color: 'var(--po-text-muted)', marginBottom: 8 }}>
                        You can try:
                      </div>
                      <ol style={{ margin: 0, paddingLeft: 20 }}>
                        {error.suggested_actions.map((action, i) => (
                          <li key={i} style={{ marginBottom: 4, color: 'var(--po-text)' }}>
                            {action}
                          </li>
                        ))}
                      </ol>
                      {error.error_code === 'RLS_BLOCKED' && (
                        <button
                          onClick={() => setShowRLSGuide(true)}
                          style={{
                            marginTop: 12,
                            height: 30,
                            padding: '0 16px',
                            borderRadius: 6,
                            border: '1px solid color-mix(in srgb, var(--po-accent) 50%, transparent)',
                            background: 'color-mix(in srgb, var(--po-accent) 10%, transparent)',
                            color: 'var(--po-text)',
                            cursor: 'pointer',
                            fontSize: 13,
                            fontWeight: 500,
                          }}
                        >
                          Configure RLS
                        </button>
                      )}
                    </div>
                  )}
                </div>
              </div>
            </div>
          )}
        </div>

        {/* Footer */}
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10, padding: '0 20px 20px' }}>
          <ActionButton
            onClick={onClose}
          >
            Cancel
          </ActionButton>
          <ActionButton
            onClick={handleConnect}
            disabled={!canSubmit || isConnecting}
            variant="primary"
            loading={isConnecting}
          >
            {isConnecting && <Dots size='xs' />}
            {isConnecting ? 'Connecting…' : 'Connect'}
          </ActionButton>
        </div>
      </div>
    </div>
  );

  // RLS Guide Modal (simplified placeholder)
  if (showRLSGuide) {
    return (
      <div
        style={{
          position: 'fixed', inset: 0, background: 'var(--po-backdrop)',
          display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000,
        }}
        onClick={() => setShowRLSGuide(false)}
      >
        <div
          style={{
            background: 'var(--po-overlay)',
            border: '1px solid var(--po-border)',
            borderRadius: 12,
            width: 600,
            maxWidth: '90vw',
            padding: 24,
          }}
        >
          <h2 style={{ margin: '0 0 16px 0', fontSize: 13, fontWeight: 500, color: 'var(--po-text-muted)' }}>
            Configure Row Level Security
          </h2>
          <p style={{ fontSize: 13, color: 'var(--po-text-muted)', lineHeight: 1.6, marginBottom: 20 }}>
            Your Supabase database has Row Level Security (RLS) enabled. Follow these steps to allow anon access:
          </p>
          <ol style={{ margin: 0, paddingLeft: 20 }}>
            <li style={{ marginBottom: 16, color: 'var(--po-text)' }}>
              <strong>1. Open Supabase SQL Editor</strong>
              <div style={{ fontSize: 12, color: 'var(--po-text-muted)', marginTop: 4 }}>
                In your Supabase Dashboard, go to{' '}
                <a
                  href="https://supabase.com/dashboard/project/_/sql"
                  target="_blank"
                  rel="noopener noreferrer"
                  style={{ color: 'var(--po-accent)', textDecoration: 'underline' }}
                >
                  SQL Editor
                </a>
              </div>
            </li>
            <li style={{ marginBottom: 16, color: 'var(--po-text)' }}>
              <strong>2. Enable RLS on your table</strong>
              <div style={{ fontSize: 12, color: 'var(--po-text-muted)', marginTop: 4 }}>
                Run this SQL command for each table you want to share:
              </div>
              <pre
                style={{
                  background: 'var(--po-inset)',
                  padding: 12,
                  borderRadius: 6,
                  fontSize: 11,
                  overflow: 'auto',
                  marginTop: 8,
                }}
              >{`ALTER TABLE your_table_name ENABLE ROW LEVEL SECURITY;`}</pre>
            </li>
            <li style={{ marginBottom: 16, color: 'var(--po-text)' }}>
              <strong>3. Create access policy</strong>
              <div style={{ fontSize: 12, color: 'var(--po-text-muted)', marginTop: 4 }}>
                Create a policy that allows anon users to read:
              </div>
              <pre
                style={{
                  background: 'var(--po-inset)',
                  padding: 12,
                  borderRadius: 6,
                  fontSize: 11,
                  overflow: 'auto',
                  marginTop: 8,
                }}
              >{`CREATE POLICY "Allow anon read" ON your_table_name
  FOR SELECT
  TO anon
  USING (true);`}</pre>
            </li>
            <li style={{ marginBottom: 16, color: 'var(--po-text)' }}>
              <strong>4. Verify the policy</strong>
              <div style={{ fontSize: 12, color: 'var(--po-text-muted)', marginTop: 4 }}>
                Test that the policy works:
              </div>
              <pre
                style={{
                  background: 'var(--po-inset)',
                  padding: 12,
                  borderRadius: 6,
                  fontSize: 11,
                  overflow: 'auto',
                  marginTop: 8,
                }}
              >{`SELECT * FROM your_table_name LIMIT 1;`}</pre>
            </li>
            <li style={{ marginBottom: 8, color: 'var(--po-text)' }}>
              <strong>5. Return and try connecting</strong>
              <div style={{ fontSize: 12, color: 'var(--po-text-muted)', marginTop: 4 }}>
                Come back to this dialog and click Connect again.
              </div>
            </li>
          </ol>
          <div style={{ marginTop: 24, textAlign: 'center' }}>
            <ActionButton
              onClick={() => setShowRLSGuide(false)}
              variant="primary"
            >
              Done
            </ActionButton>
          </div>
        </div>
      </div>
    );
  }
}
