import type { SynchronizeProviderSpec as ConnectorSpec } from './synchronizeApi';

export type SyncModeType = 'manual' | 'scheduled' | 'realtime';

export const SYNC_MODE_META: Record<SyncModeType, { label: string; desc: string }> = {
  manual: { label: 'Manual', desc: 'Sync on demand' },
  scheduled: { label: 'Scheduled', desc: 'Sync on a recurring schedule' },
  realtime: { label: 'Real-time', desc: 'Sync continuously as changes happen' },
};

interface TriggerPolicy {
  supportedModes: SyncModeType[];
  defaultMode: SyncModeType;
}

// Static fallback — guarantees correct behaviour even before the API responds
// or if the network request fails. API specs take priority when available.
const PROVIDER_POLICIES: Record<string, TriggerPolicy> = {
  gmail:      { supportedModes: ['manual', 'scheduled'], defaultMode: 'manual' },
  google_calendar: { supportedModes: ['manual', 'scheduled'], defaultMode: 'manual' },
  google_sheets:   { supportedModes: ['manual', 'scheduled'], defaultMode: 'manual' },
  google_docs:     { supportedModes: ['manual', 'scheduled'], defaultMode: 'manual' },
  url:             { supportedModes: ['manual', 'scheduled'], defaultMode: 'manual' },
  google_drive:    { supportedModes: ['manual', 'scheduled'], defaultMode: 'manual' },
  google_search_console: { supportedModes: ['manual', 'scheduled'], defaultMode: 'scheduled' },
};

const DEFAULT_POLICY: TriggerPolicy = {
  supportedModes: ['manual'],
  defaultMode: 'manual',
};

const PROVIDER_DISPLAY_LABELS: Record<string, string> = {
  gmail: 'Gmail',
  google_calendar: 'Google Calendar',
  google_sheets: 'Google Sheets',
  google_docs: 'Google Docs',
  github: 'GitHub',
  url: 'Web Page',
  google_drive: 'Google Drive',
  google_search_console: 'Google Search Console',
  agent: 'Agent',
  mcp: 'MCP Server',
  sandbox: 'Sandbox',
};

export function getSyncTriggerPolicy(
  provider: string,
  specs?: ConnectorSpec[],
): TriggerPolicy {
  if (specs && specs.length > 0) {
    const spec = specs.find(s => s.provider === provider);
    if (spec) {
      const supportedModes = (spec.supported_sync_modes || [])
        .filter((mode): mode is SyncModeType => mode === 'manual' || mode === 'scheduled' || mode === 'realtime');
      return {
        supportedModes: supportedModes.length ? supportedModes : DEFAULT_POLICY.supportedModes,
        defaultMode: supportedModes.includes(spec.default_sync_mode as SyncModeType)
          ? spec.default_sync_mode as SyncModeType
          : supportedModes[0] || DEFAULT_POLICY.defaultMode,
      };
    }
  }
  return PROVIDER_POLICIES[provider] || DEFAULT_POLICY;
}

export function getProviderDisplayLabel(
  provider: string,
  specs?: ConnectorSpec[],
): string {
  if (specs && specs.length > 0) {
    const spec = specs.find(s => s.provider === provider);
    if (spec) return spec.display_name;
  }
  return PROVIDER_DISPLAY_LABELS[provider] || provider;
}

/**
 * Build a provider → display_name lookup from ConnectorSpec[].
 * Useful for components that need the map as a Record.
 */
export function buildProviderLabels(specs: ConnectorSpec[]): Record<string, string> {
  const map: Record<string, string> = {};
  for (const s of specs) {
    map[s.provider] = s.display_name;
  }
  map['agent'] = 'Agent';
  map['mcp'] = 'MCP Server';
  map['sandbox'] = 'Sandbox';
  return map;
}
