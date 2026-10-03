'use client';

import { useState, useEffect } from 'react';
import {
  listImportDatabaseTables,
  previewImportDatabaseTable,
  saveImportDatabaseTable,
  type ImportDatabaseTable,
  type ImportDatabasePreview,
} from '../lib/importDatabaseApi';
import { Dots, PageLoading } from './loading';
import { ActivityIconButton } from './ActivityIconButton';

type SupabaseTablePickerDialogProps = {
  projectId: string;
  importDatabaseSourceId: string;
  onClose: () => void;
  onSaved?: () => void;
};

/**
 * Supabase Table Picker - 选表 → 预览 → 保存
 *
 * 导出名保持 SupabaseSQLEditorDialog 以兼容现有 import。
 */
export function SupabaseSQLEditorDialog({ projectId, importDatabaseSourceId, onClose, onSaved }: SupabaseTablePickerDialogProps) {
  const [tables, setTables] = useState<ImportDatabaseTable[]>([]);
  const [tablesLoading, setTablesLoading] = useState(true);
  const [selectedTable, setSelectedTable] = useState<string | null>(null);
  const [manualTableInput, setManualTableInput] = useState('');
  const [preview, setPreview] = useState<ImportDatabasePreview | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saveSuccess, setSaveSuccess] = useState<string | null>(null);
  const [copySqlStatus, setCopySqlStatus] = useState<'idle' | 'copied' | 'failed'>('idle');

  // Load tables on mount
  useEffect(() => {
    listImportDatabaseTables(importDatabaseSourceId)
      .then(t => { setTables(t); setTablesLoading(false); })
      .catch(err => {
        const message = String(err);
        if (message.includes('ANON_INTROSPECTION_RESTRICTED')) {
          setError('This project does not allow anon-key table introspection. You can still enter a table name manually below and preview it.');
        } else {
          setError(message);
        }
        setTablesLoading(false);
      });
  }, [importDatabaseSourceId]);

  // Load preview when table selected
  useEffect(() => {
    if (!selectedTable) { setPreview(null); return; }
    setPreviewLoading(true);
    setError(null);
    setSaveSuccess(null);
    previewImportDatabaseTable(importDatabaseSourceId, selectedTable, 50)
      .then(p => { setPreview(p); setPreviewLoading(false); })
      .catch(err => { setError(String(err)); setPreviewLoading(false); });
  }, [selectedTable, importDatabaseSourceId]);

  const handleSave = async () => {
    if (!selectedTable) return;
    setIsSaving(true);
    setError(null);
    setSaveSuccess(null);

    try {
      const res = await saveImportDatabaseTable(importDatabaseSourceId, projectId, {
        name: selectedTable,
        table: selectedTable,
      });
      setSaveSuccess(`Saved "${selectedTable}" (${res.row_count} rows)`);
      onSaved?.();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setIsSaving(false);
    }
  };

  const handleManualPreview = () => {
    const table = manualTableInput.trim();
    if (!table) return;
    setSelectedTable(table);
  };

  const rlsSuggestionSql = selectedTable
    ? `ALTER TABLE ${selectedTable} ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Allow anon read" ON ${selectedTable}
  FOR SELECT
  TO anon
  USING (true);`
    : '';

  const handleCopySql = async () => {
    if (!rlsSuggestionSql) return;
    try {
      if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(rlsSuggestionSql);
      } else {
        // Fallback for environments without Clipboard API
        const textarea = document.createElement('textarea');
        textarea.value = rlsSuggestionSql;
        textarea.style.position = 'fixed';
        textarea.style.opacity = '0';
        document.body.appendChild(textarea);
        textarea.focus();
        textarea.select();
        document.execCommand('copy');
        document.body.removeChild(textarea);
      }
      setCopySqlStatus('copied');
    } catch {
      setCopySqlStatus('failed');
    }
    setTimeout(() => setCopySqlStatus('idle'), 1800);
  };

  // Get column info for selected table
  const selectedTableInfo = tables.find(t => t.name === selectedTable);

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
          width: 820,
          maxWidth: '95vw',
          height: 560,
          maxHeight: '90vh',
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
        `}</style>

        {/* Header */}
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '14px 20px 8px', flexShrink: 0 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <svg width="18" height="18" viewBox="0 0 109 113" fill="none">
              <path d="M63.7076 110.284C60.8481 113.885 55.0502 111.912 54.9813 107.314L53.9738 40.0627L99.1935 40.0627C107.384 40.0627 111.952 49.5228 106.859 55.9374L63.7076 110.284Z" fill="url(#sp2)"/>
              <path d="M45.317 2.07103C48.1765 -1.53037 53.9745 0.442937 54.0434 5.041L54.4849 72.2922H9.83113C1.64038 72.2922 -2.92775 62.8321 2.1655 56.4175L45.317 2.07103Z" fill="#3ECF8E"/>
              <defs><linearGradient id="sp2" x1="53.9738" y1="54.974" x2="94.1635" y2="71.8295" gradientUnits="userSpaceOnUse"><stop stopColor="#249361"/><stop offset="1" stopColor="#3ECF8E"/></linearGradient></defs>
            </svg>
            <span style={{ color: 'var(--po-text-muted)', fontSize: 13, fontWeight: 500 }}>Select Table to Import</span>
          </div>
          <ActivityIconButton kind="close" title="Close" onClick={onClose} />
        </div>

        {/* Body: sidebar + preview */}
        <div style={{ display: 'flex', flex: 1, overflow: 'hidden', minHeight: 0 }}>
          {/* Left: Table list */}
          <div style={{ width: 220, borderRight: '1px solid var(--po-border-subtle)', display: 'flex', flexDirection: 'column', flexShrink: 0 }}>
            <div style={{ padding: '10px 14px 6px', fontSize: 11, fontWeight: 600, color: 'var(--po-text-subtle)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
              Tables ({tables.length})
            </div>
            <div style={{ padding: '0 12px 10px', borderBottom: '1px solid var(--po-border-subtle)' }}>
              <input
                type="text"
                value={manualTableInput}
                onChange={e => setManualTableInput(e.target.value)}
                onKeyDown={e => { if (e.key === 'Enter') handleManualPreview(); }}
                placeholder="Manual table name"
                style={{
                  width: '100%',
                  background: 'var(--po-hover)',
                  border: '1px solid var(--po-active)',
                  borderRadius: 6,
                  padding: '7px 8px',
                  color: 'var(--po-text)',
                  fontSize: 12,
                  outline: 'none',
                }}
              />
              <button
                onClick={handleManualPreview}
                disabled={!manualTableInput.trim()}
                style={{
                  marginTop: 8,
                  width: '100%',
                  height: 30,
                  padding: '0 8px',
                  borderRadius: 6,
                  border: '1px solid color-mix(in srgb, #3ECF8E 40%, transparent)',
                  background: manualTableInput.trim() ? 'color-mix(in srgb, #3ECF8E 12%, transparent)' : 'color-mix(in srgb, #3ECF8E 5%, transparent)',
                  color: manualTableInput.trim() ? 'var(--po-success)' : 'var(--po-text-disabled)',
                  cursor: manualTableInput.trim() ? 'pointer' : 'not-allowed',
                  fontSize: 12,
                }}
              >
                Preview manual table
              </button>
            </div>
            <div style={{ flex: 1, overflowY: 'auto' }}>
              {tablesLoading && (
                <div style={{ height: 96, display: 'flex' }}>
                  <PageLoading variant="fill" label="Loading tables" />
                </div>
              )}
              {tables.map(t => (
                <div
                  key={t.name}
                  onClick={() => setSelectedTable(t.name)}
                  style={{
                    padding: '8px 14px',
                    fontSize: 13,
                    color: selectedTable === t.name ? 'var(--po-text)' : 'var(--po-text-muted)',
                    background: selectedTable === t.name ? 'color-mix(in srgb, var(--po-success) 8%, transparent)' : 'transparent',
                    cursor: 'pointer',
                    borderLeft: selectedTable === t.name ? '2px solid #3ECF8E' : '2px solid transparent',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                  }}
                  onMouseEnter={e => { if (selectedTable !== t.name) { e.currentTarget.style.background = 'var(--po-hover)'; } }}
                  onMouseLeave={e => { if (selectedTable !== t.name) { e.currentTarget.style.background = 'transparent'; } }}
                >
                  <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{t.name}</span>
                  <span style={{ fontSize: 11, color: 'var(--po-text-disabled)', flexShrink: 0, marginLeft: 6 }}>
                    {t.columns.length}
                  </span>
                </div>
              ))}
              {!tablesLoading && tables.length === 0 && (
                <div style={{ padding: '12px 14px', fontSize: 13, color: 'var(--po-text-disabled)', lineHeight: 1.5 }}>
                  No auto-discovered tables.
                  <br />
                  Try manual table name above.
                </div>
              )}
            </div>
          </div>

          {/* Right: Preview */}
          <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0 }}>
            {/* No table selected */}
            {!selectedTable && (
              <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                <div style={{ textAlign: 'center', color: 'var(--po-text-disabled)' }}>
                  <div style={{ fontSize: 28, marginBottom: 8 }}>←</div>
                  <div style={{ fontSize: 14 }}>Select a table to preview</div>
                </div>
              </div>
            )}

            {/* Loading */}
            {selectedTable && previewLoading && (
              <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                <PageLoading variant="fill" label="Loading preview" />
              </div>
            )}

            {/* Preview data */}
            {selectedTable && preview && !previewLoading && (
              <>
                {/* Column info */}
                {selectedTableInfo && (
                  <div style={{ padding: '8px 16px', borderBottom: '1px solid var(--po-hover)', flexShrink: 0 }}>
                    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
                      {selectedTableInfo.columns.map(c => (
                        <span key={c.name} style={{ padding: '2px 8px', background: 'var(--po-hover)', borderRadius: 4, fontSize: 11, color: 'var(--po-text-subtle)' }}>
                          {c.name} <span style={{ color: 'var(--po-text-disabled)' }}>{c.type}</span>
                        </span>
                      ))}
                    </div>
                  </div>
                )}

                {/* Empty rows guidance */}
                {preview.row_count === 0 && (
                  <div
                    style={{
                      margin: '12px 16px 0',
                      padding: '12px',
                      background: 'var(--po-selected)',
                      border: '1px solid var(--po-focus-ring)',
                      borderRadius: 8,
                    }}
                  >
                    <div style={{ fontSize: 13, fontWeight: 600, color: 'var(--po-accent-text)', marginBottom: 6 }}>
                      No rows returned (possible RLS policy issue)
                    </div>
                    <div style={{ fontSize: 12, color: 'var(--po-text)', lineHeight: 1.5, marginBottom: 8 }}>
                      The table may contain data, but anon access can still return 0 rows when RLS is enabled
                      and no SELECT policy exists for role <code>anon</code>.
                    </div>
                    <div style={{ fontSize: 12, color: 'var(--po-text-muted)', marginBottom: 6 }}>
                      Try this in Supabase SQL Editor (replace with your final secure policy later):
                    </div>
                    <div style={{ position: 'relative' }}>
                      <button
                        onClick={handleCopySql}
                        aria-label="Copy SQL"
                        title="Copy SQL"
                        style={{
                          position: 'absolute',
                          top: 8,
                          right: 8,
                          zIndex: 1,
                          width: 24,
                          height: 24,
                          display: 'flex',
                          alignItems: 'center',
                          justifyContent: 'center',
                          borderRadius: 6,
                          border: '1px solid color-mix(in srgb, var(--po-text-muted) 35%, transparent)',
                          background: 'var(--po-inset)',
                          color: 'var(--po-text-muted)',
                          cursor: 'pointer',
                          fontSize: 12,
                        }}
                      >
                        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                          <rect x="9" y="9" width="10" height="10" rx="2" stroke="currentColor" strokeWidth="2" />
                          <path d="M5 15V7a2 2 0 0 1 2-2h8" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
                        </svg>
                      </button>
                      <pre
                        style={{
                          background: 'var(--po-inset)',
                          border: '1px solid var(--po-border)',
                          borderRadius: 6,
                          padding: '38px 10px 10px',
                          margin: 0,
                          fontSize: 11,
                          color: 'var(--po-text)',
                          overflow: 'auto',
                        }}
                      >{rlsSuggestionSql}</pre>
                    </div>
                    <div style={{ marginTop: 6, fontSize: 12, color: copySqlStatus === 'failed' ? 'var(--po-danger)' : 'var(--po-accent-text)' }}>
                      {copySqlStatus === 'copied' ? 'Copied' : copySqlStatus === 'failed' ? 'Copy failed' : ''}
                    </div>
                  </div>
                )}

                {/* Data table */}
                <div style={{ flex: 1, overflow: 'auto', minHeight: 0 }}>
                  <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
                    <thead>
                      <tr style={{ position: 'sticky', top: 0, background: 'var(--po-overlay)', zIndex: 1 }}>
                        {preview.columns.map(col => (
                          <th key={col} style={{ padding: '8px 12px', textAlign: 'left', fontWeight: 600, color: 'var(--po-text-muted)', borderBottom: '1px solid var(--po-border)', whiteSpace: 'nowrap' }}>
                            {col}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {preview.rows.map((row, idx) => (
                        <tr key={idx} style={{ borderBottom: '1px solid var(--po-hover)' }}
                          onMouseEnter={e => e.currentTarget.style.background = 'var(--po-panel)'}
                          onMouseLeave={e => e.currentTarget.style.background = 'transparent'}
                        >
                          {preview.columns.map(col => (
                            <td key={col} style={{
                              padding: '6px 12px', color: 'var(--po-text-muted)', maxWidth: 250, overflow: 'hidden',
                              textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                              fontFamily: 'var(--po-font-sans)',
                            }} title={String(row[col] ?? '')}>
                              {row[col] === null
                                ? <span style={{ color: 'var(--po-text-disabled)', fontStyle: 'italic' }}>null</span>
                                : String(row[col])}
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>

                {/* Footer: row count + save */}
                <div style={{ padding: '10px 16px', display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexShrink: 0 }}>
                  <span style={{ fontSize: 12, color: 'var(--po-text-subtle)' }}>
                    Showing {preview.row_count} rows · {preview.execution_time_ms.toFixed(0)}ms
                  </span>

                  <button
                    onClick={handleSave}
                    disabled={isSaving}
                    style={{
                      height: 30, padding: '0 20px', borderRadius: 8, border: 'none',
                      background: isSaving ? 'color-mix(in srgb, #3ECF8E 30%, transparent)' : '#3ECF8E',
                      color: isSaving ? 'var(--po-text-disabled)' : '#06130c',
                      cursor: isSaving ? 'not-allowed' : 'pointer',
                      fontSize: 13, fontWeight: 600,
                      display: 'inline-flex', alignItems: 'center', gap: 6,
                    }}
                  >
                    {isSaving && <Dots size='xs' />}
                    {isSaving ? 'Saving…' : `Save "${selectedTable}" to Project`}
                  </button>
                </div>
              </>
            )}

            {/* Error */}
            {error && (
              <div style={{ margin: '12px 16px', padding: '10px 12px', background: 'color-mix(in srgb, var(--po-danger) 10%, transparent)', border: '1px solid color-mix(in srgb, var(--po-danger) 20%, transparent)', borderRadius: 8, color: 'var(--po-danger)', fontSize: 13 }}>
                {error}
              </div>
            )}

            {/* Save success */}
            {saveSuccess && (
              <div style={{ margin: '12px 16px', padding: '10px 12px', background: 'color-mix(in srgb, var(--po-success) 10%, transparent)', border: '1px solid color-mix(in srgb, var(--po-success) 20%, transparent)', borderRadius: 8, color: 'var(--po-success)', fontSize: 13 }}>
                {saveSuccess}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
