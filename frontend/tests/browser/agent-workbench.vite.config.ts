import { defineConfig } from 'vite';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('../..', import.meta.url));
const ports = fileURLToPath(new URL('./agent-workbench.ports.tsx', import.meta.url));
export default defineConfig({
  root,
  resolve: { alias: [
    ...['@/contexts/SupabaseAuthProvider', '@/contexts/AgentContext', '@/lib/hooks/useOnboarding', '@/lib/hooks/useData', '@/lib/apiClient']
      .map(find => ({ find, replacement: ports })),
    { find: '@', replacement: root },
  ] },
  oxc: { jsx: { runtime: 'automatic' } },
  server: { host: '127.0.0.1', port: 4176, strictPort: true, watch: { ignored: ['**/.next*/**'] } },
});
