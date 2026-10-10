/**
 * Feature flags — temporary toggles for product surfaces we want to
 * pull from the UI without ripping the underlying code out.
 *
 * Each flag below has a comment block documenting:
 *   - WHEN it was hidden,
 *   - WHY (the product decision behind it),
 *   - WHAT to do to re-enable.
 *
 * Flag-gated code paths are still live — the components, contexts,
 * routes, API clients, DB tables, and backend endpoints all remain
 * intact. We only stop *rendering the entry points*, so the user
 * can't reach the hidden surface from the UI. This makes a future
 * decision to bring a feature back a one-line edit (flip the flag)
 * rather than a multi-day re-implementation.
 *
 * If you find yourself wanting to thread a per-flag prop through a
 * dozen components, prefer importing the flag at the leaf component
 * and gating there — that keeps the call sites clean and keeps
 * "what's hidden right now" answerable by grepping this file plus
 * `import { ... } from '@/lib/featureFlags'`.
 */

/**
 * AI Agent (in-app chat agent) — hidden 2026-05-08.
 *
 * ## Why hidden
 *
 * Puppyone's product positioning is **"cloud file system FOR AI
 * agents"** — a platform / data layer that external agents (Claude
 * Desktop, Cursor, custom MCP clients, etc.) read and write through
 * Terminal CLI, Local Sync, and MCP. That's a clean infra story:
 * we are the storage, version control, ACL, and audit trail; LLM
 * runtimes are someone else's product.
 *
 * Shipping an in-app chat agent alongside that pitch creates two
 * problems:
 *
 *   1. **Positioning ambiguity.** It's hard to be both "the layer
 *      under every agent" and "an agent product". A pitch that
 *      requires three sentences to explain "we are infra, but also
 *      a product, but you can also bring your own" isn't a
 *      shippable pitch.
 *
 *   2. **Quality bar.** A small team competing with Anthropic /
 *      OpenAI / Cursor on chat-agent UX will lose. The chat surface
 *      will always feel half-finished compared to dedicated
 *      products, which makes the platform feel half-finished by
 *      association.
 *
 * The Connect section reads more cleanly with just Terminal CLI +
 * Local Sync — both are pure exposure mechanisms. AI Agent was
 * conceptually different (a *consumer* of the data, not an
 * exposure path) and was creating mental clutter in the UI even
 * before the quality concern.
 *
 * ## What's still alive
 *
 *   - `contexts/AgentContext.tsx`, `components/agent/views/*`,
 *     `features/agent/runtime/` — durable run observation and commands
 *     primitives and data hooks remain.
 *   - `app/api/backend/[...path]/route.ts` — authenticated HTTP/SSE proxy.
 *   - Backend `/api/v1/agents`, `/api/v1/agent-config`,
 *     `agent_profiles`, `chat_sessions`, `chat_messages` tables —
 *     untouched.
 *   - `app/(main)/projects/[projectId]/toolkit/page.tsx` — route
 *     file still exists, just no nav link to it.
 *
 * ## What's hidden (entry points only)
 *
 *   - `ConnectMethods.tsx` AI Agent MethodCard
 *   - `AccessPointRow.tsx` agent MethodChip + agent integration row
 *   - `home/components/AccessPointsCard.tsx` agent provider rows
 *   - `onboarding/GettingStartedPanel.tsx` "Create an AI Agent" /
 *     "Chat with your Agent" steps
 *   - `access/components/*` agent connector cards (in the new
 *     access route)
 *   - `DataPageRightPanel.tsx` `agent_chat` view branch (defensive
 *     fallback in case stale state ever points there)
 *
 * ## To re-enable
 *
 * Set this constant to `true`. All gated code paths come back.
 * Verify the connect section, overview chips, and onboarding steps
 * render the agent surface again before shipping.
 */
export const AI_AGENT_ENABLED = false;

/**
 * Project Logs — hidden 2026-06-16.
 *
 * ## Why hidden
 *
 * The logs page is still early and the main project sidebar should stay
 * focused on stable, high-confidence workflows. Keeping the route alive
 * lets us keep iterating internally without putting a half-ready surface
 * in front of every project user.
 *
 * ## What's still alive
 *
 *   - `app/(main)/projects/[projectId]/develop/logs/page.tsx`
 *   - `app/(main)/projects/[projectId]/develop/*` layouts/redirects
 *   - Project audit log API clients and backend endpoints
 *
 * ## What's hidden (entry points only)
 *
 *   - `AppSidebar.tsx` project sidebar `Develop` item, which routes to
 *     `/projects/{projectId}/develop/logs`.
 *
 * ## To re-enable
 *
 * Set this constant to `true` and verify the project sidebar, collapsed
 * rail, and `/develop/logs` page still read cleanly together.
 */
export const PROJECT_LOGS_ENABLED = false;
